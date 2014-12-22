
import re
import posixpath
from pprint import pformat

from werkzeug.urls import url_encode, url_quote, url_join
from werkzeug.utils import redirect, format_string
from werkzeug.exceptions import HTTPException, NotFound, MethodNotAllowed
from werkzeug._internal import _get_environ, _encode_idna
from werkzeug._compat import itervalues, iteritems, to_unicode, to_bytes, \
     text_type, string_types, native_string_result, \
     implements_to_string, wsgi_decoding_dance
from werkzeug.datastructures import ImmutableDict, MultiDict


_rule_re = re.compile(r'''
    (?P<static>[^<]*)                           # static rule data
    <
    (?:
        (?P<converter>[a-zA-Z_][a-zA-Z0-9_]*)   # converter name
        (?:\((?P<args>.*?)\))?                  # converter arguments
        \:                                      # variable delimiter
    )?
    (?P<variable>[a-zA-Z_][a-zA-Z0-9_]*)        # variable name
    >
''', re.VERBOSE)
_simple_rule_re = re.compile(r'<([^>]+)>')
_converter_args_re = re.compile(r'''
    ((?P<name>\w+)\s*=\s*)?
    (?P<value>
        True|False|
        \d+.\d+|
        \d+.|
        \d+|
        \w+|
        [urUR]?(?P<stringval>"[^"]*?"|'[^']*')
    )\s*,
''', re.VERBOSE | re.UNICODE)


_PYTHON_CONSTANTS = {
    'None':     None,
    'True':     True,
    'False':    False
}


def _pythonize(value):
    if value in _PYTHON_CONSTANTS:
        return _PYTHON_CONSTANTS[value]
    for convert in int, float:
        try:
            return convert(value)
        except ValueError:
            pass
    if value[:1] == value[-1:] and value[0] in '"\'':
        value = value[1:-1]
    return text_type(value)


def parse_converter_args(argstr):
    argstr += ','
    args = []
    kwargs = {}

    for item in _converter_args_re.finditer(argstr):
        value = item.group('stringval')
        if value is None:
            value = item.group('value')
        value = _pythonize(value)
        if not item.group('name'):
            args.append(value)
        else:
            name = item.group('name')
            kwargs[name] = value

    return tuple(args), kwargs


class EndpointPrefix(RuleFactory):
    """Prefixes all endpoints (which must be strings for this factory) with
    another string. This can be useful for sub applications::

        url_map = Map([
            Rule('/', endpoint='index'),
            EndpointPrefix('blog/', [Submount('/blog', [
                Rule('/', endpoint='index'),
                Rule('/entry/<entry_slug>', endpoint='show')
            ])])
        ])
    """

    def __init__(self, prefix, rules):
        self.prefix = prefix
        self.rules = rules

    def get_rules(self, map):
        for rulefactory in self.rules:
            for rule in rulefactory.get_rules(map):
                rule = rule.empty()
                rule.endpoint = self.prefix + rule.endpoint
                yield rule


class Map(object):

    def __init__(self, rules=None, default_subdomain='', charset='utf-8',
                 strict_slashes=True, redirect_defaults=True,
                 converters=None, sort_parameters=False, sort_key=None,
                 encoding_errors='replace', host_matching=False):
        self._rules = []
        self._rules_by_endpoint = {}
        self._remap = True

        self.default_subdomain = default_subdomain
        self.charset = charset
        self.encoding_errors = encoding_errors
        self.strict_slashes = strict_slashes
        self.redirect_defaults = redirect_defaults
        self.host_matching = host_matching

        self.converters = self.default_converters.copy()
        if converters:
            self.converters.update(converters)

        self.sort_parameters = sort_parameters
        self.sort_key = sort_key

        for rulefactory in rules or ():
            self.add(rulefactory)

    def is_endpoint_expecting(self, endpoint, *arguments):
        """Iterate over all rules and check if the endpoint expects
        the arguments provided.  This is for example useful if you have
        some URLs that expect a language code and others that do not and
        you want to wrap the builder a bit so that the current language
        code is automatically added if not provided but endpoints expect
        it.

        :param endpoint: the endpoint to check.
        :param arguments: this function accepts one or more arguments
                          as positional arguments.  Each one of them is
                          checked.
        """
        self.update()
        arguments = set(arguments)
        for rule in self._rules_by_endpoint[endpoint]:
            if arguments.issubset(rule.arguments):
                return True
        return False

    def iter_rules(self, endpoint=None):
        """Iterate over all rules or the rules of an endpoint.

        :param endpoint: if provided only the rules for that endpoint
                         are returned.
        :return: an iterator
        """
        self.update()
        if endpoint is not None:
            return iter(self._rules_by_endpoint[endpoint])
        return iter(self._rules)

    def add(self, rulefactory):
        """Add a new rule or factory to the map and bind it.  Requires that the
        rule is not bound to another map.

        :param rulefactory: a :class:`Rule` or :class:`RuleFactory`
        """
        for rule in rulefactory.get_rules(self):
            rule.bind(self)
            self._rules.append(rule)
            self._rules_by_endpoint.setdefault(rule.endpoint, []).append(rule)
        self._remap = True

    def bind(self, server_name, script_name=None, subdomain=None,
             url_scheme='http', default_method='GET', path_info=None,
             query_args=None):
        """Return a new :class:`MapAdapter` with the details specified to the
        call.  Note that `script_name` will default to ``'/'`` if not further
        specified or `None`.  The `server_name` at least is a requirement
        because the HTTP RFC requires absolute URLs for redirects and so all
        redirect exceptions raised by Werkzeug will contain the full canonical
        URL.

        If no path_info is passed to :meth:`match` it will use the default path
        info passed to bind.  While this doesn't really make sense for
        manual bind calls, it's useful if you bind a map to a WSGI
        environment which already contains the path info.

        `subdomain` will default to the `default_subdomain` for this map if
        no defined. If there is no `default_subdomain` you cannot use the
        subdomain feature.

        .. versionadded:: 0.7
           `query_args` added

        .. versionadded:: 0.8
           `query_args` can now also be a string.
        """
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
        return MapAdapter(self, server_name, script_name, subdomain,
                          url_scheme, path_info, default_method, query_args)

    def update(self):
        """Called before matching and building to keep the compiled rules
        in the correct order after things changed.
        """
        if self._remap:
            self._rules.sort(key=lambda x: x.match_compare_key())
            for rules in itervalues(self._rules_by_endpoint):
                rules.sort(key=lambda x: x.build_compare_key())
            self._remap = False


class MapAdapter(object):
    """Returned by :meth:`Map.bind` or :meth:`Map.bind_to_environ` and does
    the URL matching and building based on runtime information.
    """

    def __init__(self, map, server_name, script_name, subdomain,
                 url_scheme, path_info, default_method, query_args=None):
        self.map = map
        self.server_name = to_unicode(server_name)
        script_name = to_unicode(script_name)
        if not script_name.endswith(u'/'):
            script_name += u'/'
        self.script_name = script_name
        self.subdomain = to_unicode(subdomain)
        self.url_scheme = to_unicode(url_scheme)
        self.path_info = to_unicode(path_info)
        self.default_method = to_unicode(default_method)
        self.query_args = query_args
       
    def test(self, path_info=None, method=None):
        """Test if a rule would match.  Works like `match` but returns `True`
        if the URL matches, or `False` if it does not exist.

        :param path_info: the path info to use for matching.  Overrides the
                          path info specified on binding.
        :param method: the HTTP method used for matching.  Overrides the
                       method specified on binding.
        """
        try:
            self.match(path_info, method)
        except RequestRedirect:
            pass
        except HTTPException:
            return False
        return True

    def allowed_methods(self, path_info=None):
        """Returns the valid methods that match for a given path.

        .. versionadded:: 0.7
        """
        try:
            self.match(path_info, method='--')
        except MethodNotAllowed as e:
            return e.valid_methods
        except HTTPException as e:
            pass
        return []

    def get_host(self, domain_part):
        """Figures out the full host name for the given domain part.  The
        domain part is a subdomain in case host matching is disabled or
        a full host name.
        """
        if self.map.host_matching:
            if domain_part is None:
                return self.server_name
            return to_unicode(domain_part, 'ascii')
        subdomain = domain_part
        if subdomain is None:
            subdomain = self.subdomain
        else:
            subdomain = to_unicode(subdomain, 'ascii')
        return (subdomain and subdomain + u'.' or u'') + self.server_name

    def get_default_redirect(self, rule, method, values, query_args):
        """A helper that returns the URL to redirect to if it finds one.
        This is used for default redirecting only.

        :internal:
        """
        assert self.map.redirect_defaults
        for r in self.map._rules_by_endpoint[rule.endpoint]:
            # every rule that comes after this one, including ourself
            # has a lower priority for the defaults.  We order the ones
            # with the highest priority up for building.
            if r is rule:
                break
            if r.provides_defaults_for(rule) and \
               r.suitable_for(values, method):
                values.update(r.defaults)
                domain_part, path = r.build(values)
                return self.make_redirect_url(
                    path, query_args, domain_part=domain_part)

    def encode_query_args(self, query_args):
        if not isinstance(query_args, string_types):
            query_args = url_encode(query_args, self.map.charset)
        return query_args

    def make_redirect_url(self, path_info, query_args=None, domain_part=None):
        """Creates a redirect URL.

        :internal:
        """
        suffix = ''
        if query_args:
            suffix = '?' + self.encode_query_args(query_args)
        return str('%s://%s/%s%s' % (
            self.url_scheme,
            self.get_host(domain_part),
            posixpath.join(self.script_name[:-1].lstrip('/'),
                           path_info.lstrip('/')),
            suffix
        ))

    def make_alias_redirect_url(self, path, endpoint, values, method, query_args):
        """Internally called to make an alias redirect URL."""
        url = self.build(endpoint, values, method, append_unknown=False,
                         force_external=True)
        if query_args:
            url += '?' + self.encode_query_args(query_args)
        assert url != path, 'detected invalid alias setting.  No canonical ' \
            'URL found'
        return url

    def _partial_build(self, endpoint, values, method, append_unknown):
        """Helper for :meth:`build`.  Returns subdomain and path for the
        rule that accepts this endpoint, values and method.

        :internal:
        """
        # in case the method is none, try with the default method first
        if method is None:
            rv = self._partial_build(endpoint, values, self.default_method,
                                     append_unknown)
            if rv is not None:
                return rv

        # default method did not match or a specific method is passed,
        # check all and go with first result.
        for rule in self.map._rules_by_endpoint.get(endpoint, ()):
            if rule.suitable_for(values, method):
                rv = rule.build(values, append_unknown)
                if rv is not None:
                    return rv

    def build(self, endpoint, values=None, method=None, force_external=False,
              append_unknown=True):
        """Building URLs works pretty much the other way round.  Instead of
        `match` you call `build` and pass it the endpoint and a dict of
        arguments for the placeholders.

        The `build` function also accepts an argument called `force_external`
        which, if you set it to `True` will force external URLs. Per default
        external URLs (include the server name) will only be used if the
        target URL is on a different subdomain.

        >>> m = Map([
        ...     Rule('/', endpoint='index'),
        ...     Rule('/downloads/', endpoint='downloads/index'),
        ...     Rule('/downloads/<int:id>', endpoint='downloads/show')
        ... ])
        >>> urls = m.bind("example.com", "/")
        >>> urls.build("index", {})
        '/'
        >>> urls.build("downloads/show", {'id': 42})
        '/downloads/42'
        >>> urls.build("downloads/show", {'id': 42}, force_external=True)
        'http://example.com/downloads/42'

        Because URLs cannot contain non ASCII data you will always get
        bytestrings back.  Non ASCII characters are urlencoded with the
        charset defined on the map instance.

        Additional values are converted to unicode and appended to the URL as
        URL querystring parameters:

        >>> urls.build("index", {'q': 'My Searchstring'})
        '/?q=My+Searchstring'

        If a rule does not exist when building a `BuildError` exception is
        raised.

        The build method accepts an argument called `method` which allows you
        to specify the method you want to have an URL built for if you have
        different methods for the same endpoint specified.

        .. versionadded:: 0.6
           the `append_unknown` parameter was added.

        :param endpoint: the endpoint of the URL to build.
        :param values: the values for the URL to build.  Unhandled values are
                       appended to the URL as query parameters.
        :param method: the HTTP method for the rule if there are different
                       URLs for different methods on the same endpoint.
        :param force_external: enforce full canonical external URLs.
        :param append_unknown: unknown parameters are appended to the generated
                               URL as query string argument.  Disable this
                               if you want the builder to ignore those.
        """
        self.map.update()
        if values:
            if isinstance(values, MultiDict):
                valueiter = values.iteritems(multi=True)
            else:
                valueiter = iteritems(values)
            values = dict((k, v) for k, v in valueiter if v is not None)
        else:
            values = {}

        rv = self._partial_build(endpoint, values, method, append_unknown)
        if rv is None:
            raise BuildError(endpoint, values, method)
        domain_part, path = rv

        host = self.get_host(domain_part)

        # shortcut this.
        if not force_external and (
            (self.map.host_matching and host == self.server_name) or
             (not self.map.host_matching and domain_part == self.subdomain)):
            return str(url_join(self.script_name, './' + path.lstrip('/')))
        return str('%s://%s%s/%s' % (
            self.url_scheme,
            host,
            self.script_name[:-1],
            path.lstrip('/')
        ))
