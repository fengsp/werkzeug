# -*- coding: utf-8 -*-
"""
    werkzeug.utils
    ~~~~~~~~~~~~~~

    This module implements various utilities for WSGI applications.  Most of
    them are used by the request and response wrappers but especially for
    middleware development it makes sense to use them without the wrappers.

    :copyright: (c) 2014 by the Werkzeug Team, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
"""
import re
import os
import sys
import pkgutil
try:
    from html.entities import name2codepoint
except ImportError:
    from htmlentitydefs import name2codepoint

from werkzeug._compat import unichr, text_type, string_types, iteritems, \
    reraise, PY2
from werkzeug._internal import _DictAccessorProperty, \
     _parse_signature, _missing


_format_re = re.compile(r'\$(?:(%s)|\{(%s)\})' % (('[a-zA-Z_][a-zA-Z0-9_]*',) * 2))
_entity_re = re.compile(r'&([^;]+);')
_filename_ascii_strip_re = re.compile(r'[^A-Za-z0-9_.-]')
_windows_device_files = ('CON', 'AUX', 'COM1', 'COM2', 'COM3', 'COM4', 'LPT1',
                         'LPT2', 'LPT3', 'PRN', 'NUL')


def get_content_type(mimetype, charset):
    """Return the full content type string with charset for a mimetype.

    If the mimetype represents text the charset will be appended as charset
    parameter, otherwise the mimetype is returned unchanged.

    :param mimetype: the mimetype to be used as content type.
    :param charset: the charset to be appended in case it was a text mimetype.
    :return: the content type.
    """
    if mimetype.startswith('text/') or \
       mimetype == 'application/xml' or \
       (mimetype.startswith('application/') and
        mimetype.endswith('+xml')):
        mimetype += '; charset=' + charset
    return mimetype


def format_string(string, context):
    """String-template format a string:

    >>> format_string('$foo and ${foo}s', dict(foo=42))
    '42 and 42s'

    This does not do any attribute lookup etc.  For more advanced string
    formattings have a look at the `werkzeug.template` module.

    :param string: the format string.
    :param context: a dict with the variables to insert.
    """
    def lookup_arg(match):
        x = context[match.group(1) or match.group(2)]
        if not isinstance(x, string_types):
            x = type(string)(x)
        return x
    return _format_re.sub(lookup_arg, string)


def secure_filename(filename):
    r"""Pass it a filename and it will return a secure version of it.  This
    filename can then safely be stored on a regular file system and passed
    to :func:`os.path.join`.  The filename returned is an ASCII only string
    for maximum portability.

    On windows system the function also makes sure that the file is not
    named after one of the special device files.

    >>> secure_filename("My cool movie.mov")
    'My_cool_movie.mov'
    >>> secure_filename("../../../etc/passwd")
    'etc_passwd'
    >>> secure_filename(u'i contain cool \xfcml\xe4uts.txt')
    'i_contain_cool_umlauts.txt'

    The function might return an empty filename.  It's your responsibility
    to ensure that the filename is unique and that you generate random
    filename if the function returned an empty one.

    .. versionadded:: 0.5

    :param filename: the filename to secure
    """
    if isinstance(filename, text_type):
        from unicodedata import normalize
        filename = normalize('NFKD', filename).encode('ascii', 'ignore')
        if not PY2:
            filename = filename.decode('ascii')
    for sep in os.path.sep, os.path.altsep:
        if sep:
            filename = filename.replace(sep, ' ')
    filename = str(_filename_ascii_strip_re.sub('', '_'.join(
                   filename.split()))).strip('._')

    # on nt a couple of special files are present in each folder.  We
    # have to ensure that the target file is not such a filename.  In
    # this case we prepend an underline
    if os.name == 'nt' and filename and \
       filename.split('.')[0].upper() in _windows_device_files:
        filename = '_' + filename

    return filename


def escape(s, quote=None):
    """Replace special characters "&", "<", ">" and (") to HTML-safe sequences.

    There is a special handling for `None` which escapes to an empty string.

    .. versionchanged:: 0.9
       `quote` is now implicitly on.

    :param s: the string to escape.
    :param quote: ignored.
    """
    if s is None:
        return ''
    elif hasattr(s, '__html__'):
        return text_type(s.__html__())
    elif not isinstance(s, string_types):
        s = text_type(s)
    if quote is not None:
        from warnings import warn
        warn(DeprecationWarning('quote parameter is implicit now'), stacklevel=2)
    s = s.replace('&', '&amp;').replace('<', '&lt;') \
        .replace('>', '&gt;').replace('"', "&quot;")
    return s


def unescape(s):
    """The reverse function of `escape`.  This unescapes all the HTML
    entities, not only the XML entities inserted by `escape`.

    :param s: the string to unescape.
    """
    def handle_match(m):
        name = m.group(1)
        if name in HTMLBuilder._entities:
            return unichr(HTMLBuilder._entities[name])
        try:
            if name[:2] in ('#x', '#X'):
                return unichr(int(name[2:], 16))
            elif name.startswith('#'):
                return unichr(int(name[1:]))
        except ValueError:
            pass
        return u''
    return _entity_re.sub(handle_match, s)


def append_slash_redirect(environ, code=301):
    """Redirect to the same URL but with a slash appended.  The behavior
    of this function is undefined if the path ends with a slash already.

    :param environ: the WSGI environment for the request that triggers
                    the redirect.
    :param code: the status code for the redirect.
    """
    new_path = environ['PATH_INFO'].strip('/') + '/'
    query_string = environ.get('QUERY_STRING')
    if query_string:
        new_path += '?' + query_string
    return redirect(new_path, code)


def validate_arguments(func, args, kwargs, drop_extra=True):
    """Check if the function accepts the arguments and keyword arguments.
    Returns a new ``(args, kwargs)`` tuple that can safely be passed to
    the function without causing a `TypeError` because the function signature
    is incompatible.  If `drop_extra` is set to `True` (which is the default)
    any extra positional or keyword arguments are dropped automatically.

    The exception raised provides three attributes:

    `missing`
        A set of argument names that the function expected but where
        missing.

    `extra`
        A dict of keyword arguments that the function can not handle but
        where provided.

    `extra_positional`
        A list of values that where given by positional argument but the
        function cannot accept.

    This can be useful for decorators that forward user submitted data to
    a view function::

        from werkzeug.utils import ArgumentValidationError, validate_arguments

        def sanitize(f):
            def proxy(request):
                data = request.values.to_dict()
                try:
                    args, kwargs = validate_arguments(f, (request,), data)
                except ArgumentValidationError:
                    raise BadRequest('The browser failed to transmit all '
                                     'the data expected.')
                return f(*args, **kwargs)
            return proxy

    :param func: the function the validation is performed against.
    :param args: a tuple of positional arguments.
    :param kwargs: a dict of keyword arguments.
    :param drop_extra: set to `False` if you don't want extra arguments
                       to be silently dropped.
    :return: tuple in the form ``(args, kwargs)``.
    """
    parser = _parse_signature(func)
    args, kwargs, missing, extra, extra_positional = parser(args, kwargs)[:5]
    if missing:
        raise ArgumentValidationError(tuple(missing))
    elif (extra or extra_positional) and not drop_extra:
        raise ArgumentValidationError(None, extra, extra_positional)
    return tuple(args), kwargs


def bind_arguments(func, args, kwargs):
    """Bind the arguments provided into a dict.  When passed a function,
    a tuple of arguments and a dict of keyword arguments `bind_arguments`
    returns a dict of names as the function would see it.  This can be useful
    to implement a cache decorator that uses the function arguments to build
    the cache key based on the values of the arguments.

    :param func: the function the arguments should be bound for.
    :param args: tuple of positional arguments.
    :param kwargs: a dict of keyword arguments.
    :return: a :class:`dict` of bound keyword arguments.
    """
    args, kwargs, missing, extra, extra_positional, \
        arg_spec, vararg_var, kwarg_var = _parse_signature(func)(args, kwargs)
    values = {}
    for (name, has_default, default), value in zip(arg_spec, args):
        values[name] = value
    if vararg_var is not None:
        values[vararg_var] = tuple(extra_positional)
    elif extra_positional:
        raise TypeError('too many positional arguments')
    if kwarg_var is not None:
        multikw = set(extra) & set([x[0] for x in arg_spec])
        if multikw:
            raise TypeError('got multiple values for keyword argument ' +
                            repr(next(iter(multikw))))
        values[kwarg_var] = extra
    elif extra:
        raise TypeError('got unexpected keyword argument ' +
                        repr(next(iter(extra))))
    return values


class ArgumentValidationError(ValueError):
    """Raised if :func:`validate_arguments` fails to validate"""

    def __init__(self, missing=None, extra=None, extra_positional=None):
        self.missing = set(missing or ())
        self.extra = extra or {}
        self.extra_positional = extra_positional or []
        ValueError.__init__(self, 'function arguments invalid.  ('
                            '%d missing, %d additional)' % (
            len(self.missing),
            len(self.extra) + len(self.extra_positional)
        ))



# circular dependencies
from werkzeug.http import quote_header_value, unquote_header_value, \
     cookie_date

# DEPRECATED
# these objects were previously in this module as well.  we import
# them here for backwards compatibility with old pickles.
from werkzeug.datastructures import MultiDict, CombinedMultiDict, \
     Headers, EnvironHeaders
from werkzeug.http import parse_cookie, dump_cookie
