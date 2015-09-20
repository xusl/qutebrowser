# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2015 Daniel Schadt
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Utils for writing a MHTML file."""

import functools
import quopri
import io

from collections import namedtuple
from base64 import b64encode
from urllib.parse import urljoin
from uuid import uuid4

from PyQt5.QtCore import QUrl
from PyQt5.QtNetwork import QNetworkRequest, QNetworkReply

from qutebrowser.utils import log, objreg


_File = namedtuple("_File",
                   "content content_type content_location transfer_encoding")


def _chunked_base64(data, maxlen=76, linesep=b"\r\n"):
    """Just like b64encode, except that it breaks long lines.

    Args:
        maxlen: Maximum length of a line, not including the line separator.
        linesep: Line separator to use as bytes.
    """
    encoded = b64encode(data)
    result = []
    for i in range(0, len(encoded), maxlen):
        result.append(encoded[i:i+maxlen])
    return linesep.join(result)

def _rn_quopri(data):
    """Return a quoted-printable representation of data."""
    orig_funcs = (quopri.b2a_qp, quopri.a2b_qp)
    # Workaround for quopri mixing \n and \r\n
    quopri.b2a_qp = quopri.a2b_qp = None
    encoded = quopri.encodestring(data)
    quopri.b2a_qp, quopri.a2b_qp = orig_funcs
    return encoded.replace(b"\n", b"\r\n")


E_NONE = (None, lambda x: x)
"""No transfer encoding, copy the bytes from input to output"""

E_BASE64 = ("base64", _chunked_base64)
"""Encode the file using base64 encoding"""

E_QUOPRI = ("quoted-printable", _rn_quopri)
"""Encode the file using MIME quoted-printable encoding."""


class MHTMLWriter(object):
    """A class for aggregating multiple files and outputting them to a MHTML
    file."""

    BOUNDARY = b"---qute-mhtml-" + str(uuid4()).encode("ascii")

    def __init__(self, root_content=None, content_location=None,
                 content_type=None):
        self.root_content = root_content
        self.content_location = content_location
        self.content_type = None

        self._files = {}

    def add_file(self, location, content, content_type=None,
                 transfer_encoding=E_BASE64):
        """Add a file to the given MHTML collection.

        Args:
            location: The original location (URL) of the file.
            content: The binary content of the file.
            content_type: The MIME-type of the content (if available)
            transfer_encoding: The transfer encoding to use for this file.
        """
        self._files[location] = _File(
            content=content, content_type=content_type,
            content_location=location, transfer_encoding=transfer_encoding,
        )

    def remove_file(self, location):
        """Remove a file.

        Args:
            location: The URL that identifies the file.
        """
        del self._files[location]

    def write_to(self, fp):
        """Output the MHTML file to the given file-like object

        Args:
            fp: The file-object, openend in "wb" mode.
        """
        self._output_header(fp)
        self._output_root_file(fp)
        for file_data in self._files.values():
            self._output_file(fp, file_data)
        fp.write(b"\r\n--")
        fp.write(self.BOUNDARY)
        fp.write(b"--")

    def _output_header(self, fp):
        if self.content_location is None:
            raise ValueError("content_location must be set")
        if self.content_type is None:
            raise ValueError("content_type must be set for the root document")

        fp.write(b"Content-Location: ")
        fp.write(self.content_location.encode("utf-8"))
        fp.write(b'\r\nContent-Type: multipart/related;boundary="')
        fp.write(self.BOUNDARY)
        fp.write(b'";type="')
        fp.write(self.content_type.encode("utf-8"))
        fp.write(b'"\r\n\r\n')

    def _output_root_file(self, fp):
        root_file = _File(
            content=self.root_content, content_type=self.content_type,
            content_location=self.content_location, transfer_encoding=E_QUOPRI,
        )
        self._output_file(fp, root_file)

    def _output_file(self, fp, file_struct):
        fp.write(b"--")
        fp.write(self.BOUNDARY)
        fp.write(b"\r\nContent-Location: ")
        fp.write(file_struct.content_location.encode("utf-8"))
        if file_struct.content_type is not None:
            fp.write(b"\r\nContent-Type: ")
            fp.write(file_struct.content_type.encode("utf-8"))
        encoding_name, encoding_func = file_struct.transfer_encoding
        if encoding_name:
            fp.write(b"\r\nContent-Transfer-Encoding: ")
            fp.write(encoding_name.encode("utf-8"))
        fp.write(b"\r\n\r\n")
        fp.write(encoding_func(file_struct.content))
        fp.write(b"\r\n\r\n")


def start_download(dest):
    """Start downloading the current page and all assets to a MHTML file.

    Args:
        dest: The filename where the resulting file should be saved.
    """
    download_manager = objreg.get("download-manager", scope="window",
                                  window="current")
    web_view = objreg.get("webview", scope="tab", tab="current")
    web_url_str = web_view.url().toString()
    web_frame = web_view.page().mainFrame()

    writer = MHTMLWriter()
    writer.root_content = web_frame.toHtml().encode("utf-8")
    writer.content_location = web_url_str
    # I've found no way of getting the content type of a QWebView, but since
    # we're using .toHtml, it's probably safe to say that the content-type is
    # HTML
    writer.content_type = "text/html"
    # Currently only downloading <link> (stylesheets), <script> (javascript) and
    # <img> (image) elements.
    elements = (web_frame.findAllElements("link") +
                web_frame.findAllElements("script") +
                web_frame.findAllElements("img"))

    loaded_urls = set()
    pending_downloads = set()

    # Callback for when a single asset is downloaded
    # closes over the local variables
    def finished(name, item):
        pending_downloads.remove(item)
        mime = item.raw_headers.get(b"Content-Type", b"")
        mime = mime.decode("ascii", "ignore")
        encode = E_QUOPRI if mime.startswith("text/") else E_BASE64
        writer.add_file(name, item.fileobj.getvalue(), mime, encode)
        if pending_downloads:
            return
        finish_file()

    def error(name, item, *args):
        pending_downloads.remove(item)
        writer.add_file(name, b"")
        if pending_downloads:
            return
        finish_file()

    def finish_file():
        # If we get here, all assets are downloaded and we're ready to finis
        # the file
        log.misc.debug("All assets downloaded, ready to finish off!")
        with open(dest, "wb") as file_output:
            writer.write_to(file_output)


    for element in elements:
        element_url = element.attribute("src")
        if not element_url:
            element_url = element.attribute("href")
        if not element_url:
            # Might be a local <script> tag or something else
            continue
        absolute_url_str = urljoin(web_url_str, element_url)
        # Prevent loading an asset twice
        if absolute_url_str in loaded_urls:
            continue
        loaded_urls.add(absolute_url_str)

        log.misc.debug("asset at %s", absolute_url_str)
        absolute_url = QUrl(absolute_url_str)

        fileobj = io.BytesIO()
        item = download_manager.get(absolute_url, fileobj=fileobj,
                                    auto_remove=True)
        pending_downloads.add(item)
        item.finished.connect(
            functools.partial(finished, absolute_url_str, item))
        item.error.connect(
            functools.partial(error, absolute_url_str, item))
        item.cancelled.connect(
            functools.partial(error, absolute_url_str, item))
