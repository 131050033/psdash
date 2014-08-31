import gevent
from gevent.monkey import patch_all
patch_all()

from werkzeug._compat import wsgi_decoding_dance, to_unicode, string_types
from werkzeug._internal import _get_environ, _encode_idna
from werkzeug.exceptions import NotFound, MethodNotAllowed
from werkzeug.urls import url_quote, url_join
from werkzeug.routing import Map, MapAdapter, RequestSlash, RequestRedirect, RequestAliasRedirect, _simple_rule_re
from gevent.pywsgi import WSGIServer
import locale
import argparse
import logging
import socket
import urllib
import urllib2
from logging import getLogger
from flask import Flask
import zerorpc
from psdash.node import LocalNode, RemoteNode
from psdash.web import fromtimestamp


logger = getLogger('psdash.run')


class DebugMapAdapter(MapAdapter):
    def match(self, path_info=None, method=None, return_rule=False,
              query_args=None):
        self.map.update()
        if path_info is None:
            path_info = self.path_info
        else:
            path_info = to_unicode(path_info, self.map.charset)
        if query_args is None:
            query_args = self.query_args
        method = (method or self.default_method).upper()

        path = u'%s|/%s' % (self.map.host_matching and self.server_name or
                            self.subdomain, path_info.lstrip('/'))

        have_match_for = set()
        for rule in self.map._rules:
            print "RULE", path, rule.rule
            try:
                rv = rule.match(path)
            except RequestSlash:
                raise RequestRedirect(self.make_redirect_url(
                    url_quote(path_info, self.map.charset,
                              safe='/:|+') + '/', query_args))
            except RequestAliasRedirect as e:
                raise RequestRedirect(self.make_alias_redirect_url(
                    path, rule.endpoint, e.matched_values, method, query_args))
            if rv is None:
                continue
            if rule.methods is not None and method not in rule.methods:
                have_match_for.update(rule.methods)
                continue

            if self.map.redirect_defaults:
                redirect_url = self.get_default_redirect(rule, method, rv,
                                                         query_args)
                if redirect_url is not None:
                    raise RequestRedirect(redirect_url)

            if rule.redirect_to is not None:
                if isinstance(rule.redirect_to, string_types):
                    def _handle_match(match):
                        value = rv[match.group(1)]
                        return rule._converters[match.group(1)].to_url(value)
                    redirect_url = _simple_rule_re.sub(_handle_match,
                                                       rule.redirect_to)
                else:
                    redirect_url = rule.redirect_to(self, **rv)
                raise RequestRedirect(str(url_join('%s://%s%s%s' % (
                    self.url_scheme,
                    self.subdomain and self.subdomain + '.' or '',
                    self.server_name,
                    self.script_name
                ), redirect_url)))

            if return_rule:
                return rule, rv
            else:
                return rule.endpoint, rv

        if have_match_for:
            raise MethodNotAllowed(valid_methods=list(have_match_for))

        print "NOT FOUND, RAISING"

        raise NotFound()


class DebugMap(Map):
    def bind(self, server_name, script_name=None, subdomain=None,
             url_scheme='http', default_method='GET', path_info=None,
             query_args=None):
        server_name = server_name.lower()
        if self.host_matching:
            if subdomain is not None:
                raise RuntimeError('host matching enabled and a '
                                   'subdomain was provided')
        elif subdomain is None:
            subdomain = self.default_subdomain
        if script_name is None:
            script_name = '/'
        server_name = _encode_idna(server_name)
        return DebugMapAdapter(self, server_name, script_name, subdomain,
                          url_scheme, path_info, default_method, query_args)

    def bind_to_environ(self, environ, server_name=None, subdomain=None):
        environ = _get_environ(environ)
        if server_name is None:
            if 'HTTP_HOST' in environ:
                server_name = environ['HTTP_HOST']
                print 'HTTP_HOST', server_name
            else:
                server_name = environ['SERVER_NAME']
                if (environ['wsgi.url_scheme'], environ['SERVER_PORT']) not \
                   in (('https', '443'), ('http', '80')):
                    server_name += ':' + environ['SERVER_PORT']
        elif subdomain is None and not self.host_matching:
            server_name = server_name.lower()
            if 'HTTP_HOST' in environ:
                wsgi_server_name = environ.get('HTTP_HOST')
            else:
                wsgi_server_name = environ.get('SERVER_NAME')
                if (environ['wsgi.url_scheme'], environ['SERVER_PORT']) not \
                   in (('https', '443'), ('http', '80')):
                    wsgi_server_name += ':' + environ['SERVER_PORT']
            wsgi_server_name = wsgi_server_name.lower()
            cur_server_name = wsgi_server_name.split('.')
            real_server_name = server_name.split('.')
            offset = -len(real_server_name)
            if cur_server_name[offset:] != real_server_name:
                print "NOT MATCHING", cur_server_name[offset:] != real_server_name
                # This can happen even with valid configs if the server was
                # accesssed directly by IP address under some situations.
                # Instead of raising an exception like in Werkzeug 0.7 or
                # earlier we go by an invalid subdomain which will result
                # in a 404 error on matching.
                subdomain = '<invalid>'
            else:
                subdomain = '.'.join(filter(None, cur_server_name[:offset]))

        def _get_wsgi_string(name):
            val = environ.get(name)
            if val is not None:
                return wsgi_decoding_dance(val, self.charset)

        print "SERVER NAME RES", server_name

        script_name = _get_wsgi_string('SCRIPT_NAME')
        path_info = _get_wsgi_string('PATH_INFO')
        query_args = _get_wsgi_string('QUERY_STRING')
        return DebugMap.bind(self, server_name, script_name,
                        subdomain, environ['wsgi.url_scheme'],
                        environ['REQUEST_METHOD'], path_info,
                        query_args=query_args)


class PsDashRunner(object):
    DEFAULT_LOG_INTERVAL = 60
    DEFAULT_NET_IO_COUNTER_INTERVAL = 3
    DEFAULT_REGISTER_INTERVAL = 60
    LOCAL_NODE = 'localhost'

    @classmethod
    def create_from_cli_args(cls):
        return cls(args=None)

    def __init__(self, config_overrides=None, args=tuple()):
        self._nodes = {}
        config = self._load_args_config(args)
        if config_overrides:
            config.update(config_overrides)
        self.app = self._create_app(config)

        self._setup_nodes()
        self._setup_logging()
        self._setup_context()

    def _get_args(cls, args):
        parser = argparse.ArgumentParser(
            description='psdash %s - system information web dashboard' % '0.5.0'
        )
        parser.add_argument(
            '-l', '--log',
            action='append',
            dest='logs',
            default=[],
            metavar='path',
            help='log files to make available for psdash. Patterns (e.g. /var/log/**/*.log) are supported. '
                 'This option can be used multiple times.'
        )
        parser.add_argument(
            '-b', '--bind',
            action='store',
            dest='bind_host',
            default='0.0.0.0',
            metavar='host',
            help='host to bind to. Defaults to 0.0.0.0 (all interfaces).'
        )
        parser.add_argument(
            '-p', '--port',
            action='store',
            type=int,
            dest='port',
            default=5000,
            metavar='port',
            help='port to listen on. Defaults to 5000.'
        )
        parser.add_argument(
            '-d', '--debug',
            action='store_true',
            dest='debug',
            help='enables debug mode.'
        )
        parser.add_argument(
            '-a', '--agent',
            action='store_true',
            dest='agent',
            help='Enables agent mode. This launches a RPC server, using zerorpc, on given bind host and port.'
        )
        parser.add_argument(
            '--register-to',
            action='store',
            dest='register_to',
            default=None,
            metavar='host:port',
            help='The psdash node running in web mode to register this agent to on start up. e.g 10.0.1.22:5000'
        )
        parser.add_argument(
            '--register-as',
            action='store',
            dest='register_as',
            default=None,
            metavar='name',
            help='The name to register as. (This will default to the node\'s hostname)'
        )

        return parser.parse_args(args)

    def _load_args_config(self, args):
        config = {}
        for k, v in vars(self._get_args(args)).iteritems():
            key = 'PSDASH_%s' % k.upper() if k != 'debug' else 'DEBUG'
            config[key] = v
        return config

    def _setup_nodes(self):
        self.add_node(LocalNode())

        nodes = self.app.config.get('PSDASH_NODES', [])
        logger.info("Registering %d nodes", len(nodes))
        for n in nodes:
            self.register_node(n['name'], n['host'], int(n['port']))

    def add_node(self, node):
        self._nodes[node.get_id()] = node

    def get_local_node(self):
        return self._nodes.get(self.LOCAL_NODE)

    def get_node(self, name):
        return self._nodes.get(name)

    def get_nodes(self):
        return self._nodes

    def register_node(self, name, host, port):
        n = RemoteNode(name, host, port)
        node = self.get_node(n.get_id())
        if node:
            n = node
            logger.debug("Updating registered node %s", n.get_id())
        else:
            logger.info("Registering %s", n.get_id())
        n.update_last_registered()
        self.add_node(n)
        return n

    def _create_app(self, config=None):
        app = Flask(__name__)
        app.url_map = DebugMap()
        app.psdash = self
        app.config.from_envvar('PSDASH_CONFIG', silent=True)

        if config and isinstance(config, dict):
            app.config.update(config)

        self._load_allowed_remote_addresses(app)

        # If the secret key is not read from the config just set it to something.
        if not app.secret_key:
            app.secret_key = 'whatisthissourcery'
        app.add_template_filter(fromtimestamp)

        from psdash.web import webapp
        prefix = app.config.get('PSDASH_URL_PREFIX')
        if prefix:
            prefix = '/' + prefix.strip('/')
            webapp.url_prefix = prefix
        app.register_blueprint(webapp)

        return app

    def _load_allowed_remote_addresses(self, app):
        key = 'PSDASH_ALLOWED_REMOTE_ADDRESSES'
        addrs = app.config.get(key)
        if not addrs:
            return

        if isinstance(addrs, (str, unicode)):
            app.config[key] = [a.strip() for a in addrs.split(',')]

    def _setup_logging(self):
        level = self.app.config.get('PSDASH_LOG_LEVEL', logging.INFO) if not self.app.debug else logging.DEBUG
        format = self.app.config.get('PSDASH_LOG_FORMAT', '%(levelname)s | %(name)s | %(message)s')

        logging.basicConfig(
            level=level,
            format=format
        )
        logging.getLogger('werkzeug').setLevel(logging.WARNING if not self.app.debug else logging.DEBUG)
        
    def _setup_workers(self):
        net_io_interval = self.app.config.get('PSDASH_NET_IO_COUNTER_INTERVAL', self.DEFAULT_NET_IO_COUNTER_INTERVAL)
        gevent.spawn_later(net_io_interval, self._net_io_counters_worker, net_io_interval)

        logs_interval = self.app.config.get('PSDASH_LOGS_INTERVAL', self.DEFAULT_LOG_INTERVAL)
        gevent.spawn_later(logs_interval, self._logs_worker, logs_interval)

        if self.app.config['PSDASH_AGENT']:
            register_interval = self.app.config.get('PSDASH_REGISTER_INTERVAL', self.DEFAULT_REGISTER_INTERVAL)
            gevent.spawn_later(register_interval, self._register_agent_worker, register_interval)

    def _setup_locale(self):
        # This set locale to the user default (usually controlled by the LANG env var)
        locale.setlocale(locale.LC_ALL, '')

    def _setup_context(self):
        self.get_local_node().net_io_counters.update()
        if 'PSDASH_LOGS' in self.app.config:
            self.get_local_node().logs.add_patterns(self.app.config['PSDASH_LOGS'])

    def _logs_worker(self, sleep_interval):
        while True:
            logger.debug("Reloading logs...")
            self.get_local_node().logs.add_patterns(self.app.config['PSDASH_LOGS'])
            gevent.sleep(sleep_interval)

    def _register_agent_worker(self, sleep_interval):
        while True:
            logger.debug("Registering agent...")
            self._register_agent()
            gevent.sleep(sleep_interval)

    def _net_io_counters_worker(self, sleep_interval):
        while True:
            logger.debug("Updating net io counters...")
            self.get_local_node().net_io_counters.update()
            gevent.sleep(sleep_interval)

    def _register_agent(self):
        register_name = self.app.config['PSDASH_REGISTER_AS']
        if not register_name:
            register_name = socket.gethostname()

        url_args = {
            'name': register_name,
            'port': self.app.config['PSDASH_PORT'],
        }
        register_url = '%s/register?%s' % (self.app.config['PSDASH_REGISTER_TO'], urllib.urlencode(url_args))

        if 'PSDASH_AUTH_USERNAME' in self.app.config and 'PSDASH_AUTH_PASSWORD' in self.app.config:
            auth_handler = urllib2.HTTPBasicAuthHandler()
            auth_handler.add_password(
                realm='psDash login required',
                uri=register_url,
                user=self.app.config['PSDASH_AUTH_USERNAME'],
                passwd=self.app.config['PSDASH_AUTH_PASSWORD']
            )
            opener = urllib2.build_opener(auth_handler)
            urllib2.install_opener(opener)

        try:
            urllib2.urlopen(register_url)
        except urllib2.HTTPError as e:
            logger.error('Failed to register agent to "%s": %s', register_url, e)

    def _run_rpc(self):
        logger.info("Starting RPC server (agent mode)")

        if self.app.config['PSDASH_REGISTER_TO']:
            self._register_agent()

        service = self.get_local_node().get_service()
        self.server = zerorpc.Server(service)
        self.server.bind('tcp://%s:%s' % (self.app.config['PSDASH_BIND_HOST'], self.app.config['PSDASH_PORT']))
        self.server.run()

    def _run_web(self):
        logger.info("Starting web server")
        log = 'default' if self.app.debug else None

        ssl_args = {}
        if self.app.config.get('PSDASH_HTTPS_KEYFILE') and self.app.config.get('PSDASH_HTTPS_CERTFILE'):
            ssl_args = {
                'keyfile': self.app.config.get('PSDASH_HTTPS_KEYFILE'),
                'certfile': self.app.config.get('PSDASH_HTTPS_CERTFILE')
            }

        self.server = WSGIServer(
            (self.app.config['PSDASH_BIND_HOST'], self.app.config['PSDASH_PORT']),
            application=self.app,
            log=log,
            **ssl_args
        )
        self.server.serve_forever()

    def run(self):
        logger.info('Starting psdash v0.4.0')

        self._setup_locale()
        self._setup_workers()

        logger.info('Listening on %s:%s',
                    self.app.config['PSDASH_BIND_HOST'],
                    self.app.config['PSDASH_PORT'])

        if self.app.config['PSDASH_AGENT']:
            return self._run_rpc()
        else:
            return self._run_web()


def main():
    r = PsDashRunner.create_from_cli_args()
    r.run()
    

if __name__ == '__main__':
    main()