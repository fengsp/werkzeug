
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
