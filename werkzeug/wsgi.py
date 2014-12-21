# -*- coding: utf-8 -*-
"""
    werkzeug.wsgi
    ~~~~~~~~~~~~~

    This module implements WSGI related helpers.

    :copyright: (c) 2014 by the Werkzeug Team, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
"""
import re
import os
import sys
import posixpath
import mimetypes
from itertools import chain
from zlib import adler32
from time import time, mktime
from datetime import datetime
from functools import partial, update_wrapper

from werkzeug._compat import iteritems, text_type, string_types, \
     implements_iterator, make_literal_wrapper, to_unicode, to_bytes, \
     wsgi_get_bytes, try_coerce_native, PY2
from werkzeug._internal import _empty_stream, _encode_idna
from werkzeug.http import is_resource_modified, http_date
from werkzeug.urls import uri_to_iri, url_quote, url_parse, url_join


def make_chunk_iter(stream, separator, limit=None, buffer_size=10 * 1024):
    """Works like :func:`make_line_iter` but accepts a separator
    which divides chunks.  If you want newline based processing
    you should use :func:`make_line_iter` instead as it
    supports arbitrary newline markers.

    .. versionadded:: 0.8

    .. versionadded:: 0.9
       added support for iterators as input stream.

    :param stream: the stream or iterate to iterate over.
    :param separator: the separator that divides chunks.
    :param limit: the limit in bytes for the stream.  (Usually
                  content length.  Not necessary if the `stream`
                  is otherwise already limited).
    :param buffer_size: The optional buffer size.
    """
    _iter = _make_chunk_iter(stream, limit, buffer_size)

    first_item = next(_iter, '')
    if not first_item:
        return

    _iter = chain((first_item,), _iter)
    if isinstance(first_item, text_type):
        separator = to_unicode(separator)
        _split = re.compile(r'(%s)' % re.escape(separator)).split
        _join = u''.join
    else:
        separator = to_bytes(separator)
        _split = re.compile(b'(' + re.escape(separator) + b')').split
        _join = b''.join

    buffer = []
    while 1:
        new_data = next(_iter, '')
        if not new_data:
            break
        chunks = _split(new_data)
        new_buf = []
        for item in chain(buffer, chunks):
            if item == separator:
                yield _join(new_buf)
                new_buf = []
            else:
                new_buf.append(item)
        buffer = new_buf
    if buffer:
        yield _join(buffer)


@implements_iterator
class LimitedStream(object):
    """Wraps a stream so that it doesn't read more than n bytes.  If the
    stream is exhausted and the caller tries to get more bytes from it
    :func:`on_exhausted` is called which by default returns an empty
    string.  The return value of that function is forwarded
    to the reader function.  So if it returns an empty string
    :meth:`read` will return an empty string as well.

    The limit however must never be higher than what the stream can
    output.  Otherwise :meth:`readlines` will try to read past the
    limit.

    .. admonition:: Note on WSGI compliance

       calls to :meth:`readline` and :meth:`readlines` are not
       WSGI compliant because it passes a size argument to the
       readline methods.  Unfortunately the WSGI PEP is not safely
       implementable without a size argument to :meth:`readline`
       because there is no EOF marker in the stream.  As a result
       of that the use of :meth:`readline` is discouraged.

       For the same reason iterating over the :class:`LimitedStream`
       is not portable.  It internally calls :meth:`readline`.

       We strongly suggest using :meth:`read` only or using the
       :func:`make_line_iter` which safely iterates line-based
       over a WSGI input stream.

    :param stream: the stream to wrap.
    :param limit: the limit for the stream, must not be longer than
                  what the string can provide if the stream does not
                  end with `EOF` (like `wsgi.input`)
    """

    def __init__(self, stream, limit):
        self._read = stream.read
        self._readline = stream.readline
        self._pos = 0
        self.limit = limit

    def __iter__(self):
        return self

    @property
    def is_exhausted(self):
        """If the stream is exhausted this attribute is `True`."""
        return self._pos >= self.limit

    def on_exhausted(self):
        """This is called when the stream tries to read past the limit.
        The return value of this function is returned from the reading
        function.
        """
        # Read null bytes from the stream so that we get the
        # correct end of stream marker.
        return self._read(0)

    def on_disconnect(self):
        """What should happen if a disconnect is detected?  The return
        value of this function is returned from read functions in case
        the client went away.  By default a
        :exc:`~werkzeug.exceptions.ClientDisconnected` exception is raised.
        """
        from werkzeug.exceptions import ClientDisconnected
        raise ClientDisconnected()

    def exhaust(self, chunk_size=1024 * 64):
        """Exhaust the stream.  This consumes all the data left until the
        limit is reached.

        :param chunk_size: the size for a chunk.  It will read the chunk
                           until the stream is exhausted and throw away
                           the results.
        """
        to_read = self.limit - self._pos
        chunk = chunk_size
        while to_read > 0:
            chunk = min(to_read, chunk)
            self.read(chunk)
            to_read -= chunk

    def read(self, size=None):
        """Read `size` bytes or if size is not provided everything is read.

        :param size: the number of bytes read.
        """
        if self._pos >= self.limit:
            return self.on_exhausted()
        if size is None or size == -1:  # -1 is for consistence with file
            size = self.limit
        to_read = min(self.limit - self._pos, size)
        try:
            read = self._read(to_read)
        except (IOError, ValueError):
            return self.on_disconnect()
        if to_read and len(read) != to_read:
            return self.on_disconnect()
        self._pos += len(read)
        return read

    def readline(self, size=None):
        """Reads one line from the stream."""
        if self._pos >= self.limit:
            return self.on_exhausted()
        if size is None:
            size = self.limit - self._pos
        else:
            size = min(size, self.limit - self._pos)
        try:
            line = self._readline(size)
        except (ValueError, IOError):
            return self.on_disconnect()
        if size and not line:
            return self.on_disconnect()
        self._pos += len(line)
        return line

    def readlines(self, size=None):
        """Reads a file into a list of strings.  It calls :meth:`readline`
        until the file is read to the end.  It does support the optional
        `size` argument if the underlaying stream supports it for
        `readline`.
        """
        last_pos = self._pos
        result = []
        if size is not None:
            end = min(self.limit, last_pos + size)
        else:
            end = self.limit
        while 1:
            if size is not None:
                size -= last_pos - self._pos
            if self._pos >= end:
                break
            result.append(self.readline(size))
            if size is not None:
                last_pos = self._pos
        return result

    def tell(self):
        """Returns the position of the stream.

        .. versionadded:: 0.9
        """
        return self._pos

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration()
        return line
