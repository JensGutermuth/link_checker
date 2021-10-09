#!/usr/bin/env python3
import argparse
import asyncio
import fnmatch
import mimetypes
import os
import posixpath
import sys
import typing
from collections import defaultdict
from contextlib import suppress
from urllib.parse import unquote, urljoin, urlsplit

import h2.exceptions
import httpcore
import httpx
from httpcore._types import URL, Headers
from lxml import etree
from lxml.cssselect import CSSSelector

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:84.0) ' + \
             'Gecko/20100101 Firefox/84.0 ' + \
             '(compatible; link_checker https://github.com/Delphinator/link_checker)'

def extract_links(html_content, url):
    html = etree.HTML(html_content)
    # obvious
    img_selector = CSSSelector('img')
    a_selector = CSSSelector('a')
    script_selector = CSSSelector('script')

    # stylesheets, rel="preload"-stuff and much more
    link_selector = CSSSelector('link')
    # HTML5 <picture>-Element
    source_selector = CSSSelector('source')

    def urls_generator():
        for img in img_selector(html):
            with suppress(KeyError):
                yield urljoin(url, img.attrib['src'])
            with suppress(KeyError):
                yield urljoin(url, img.attrib['data-src'])
            with suppress(KeyError):
                # format: url [width], ...
                for src in img.attrib['srcset'].split(','):
                    src = src.strip().split()[0]
                    yield urljoin(url, src)

        for a in a_selector(html):
            with suppress(KeyError):
                yield urljoin(url, a.attrib['href'])

        for s in script_selector(html):
            with suppress(KeyError):
                yield urljoin(url, s.attrib['src'])

        for l in link_selector(html):
            with suppress(KeyError):
                yield urljoin(url, l.attrib['href'])

        for s in source_selector(html):
            with suppress(KeyError):
                yield urljoin(url, s.attrib['src'])
            with suppress(KeyError):
                # format: url [width], ...
                for src in s.attrib['srcset'].split(','):
                    src = src.strip().split()[0]
                    yield urljoin(url, src)

    return set(u.split('#')[0] for u in urls_generator())


async def check_urls(client: httpx.AsyncClient, urls: typing.Iterable,
                     recurse_in_domains: typing.Collection[str], user_agent: str,
                     ignore: typing.Collection[str], ignore_glob: typing.Collection[str],
                     ignore_http_codes: typing.Collection[int]):
    request_for = defaultdict(asyncio.BoundedSemaphore)

    async def make_request(url, attempt: int = 1):
        try:
            async with request_for[urlsplit(url).hostname]:
                r = await client.get(
                    url, headers={b"User-Agent": user_agent.encode("ascii")})
        except (httpx.NetworkError, httpx.TimeoutException,
                httpx.ProtocolError, httpx.TooManyRedirects) as e:
            err_str = str(e)
            if not err_str:
                err_str = "Error: " + repr(e)
            return url, None, err_str
        except h2.exceptions.ProtocolError as e:
            err_str = str(e)
            if 'RECV_HEADERS' not in err_str or 'CLOSED' not in err_str:
                raise
            # The full error is
            # h2.exceptions.ProtocolError: Invalid input ConnectionInputs.RECV_HEADERS in state ConnectionState.CLOSED
            # This happens if we've exceeded the number of requests the server will
            # handle using this connection. The default for nginx is apparently 1000,
            # which is very possible to hit. If this happens our best guess is to just
            # try again.

            if attempt >= 3:
                # we've tried three times? That can't be right...
                raise

            return await make_request(url, attempt=attempt+1)

        if r.status_code != httpx.codes.OK and r.status_code not in ignore_http_codes:
            return url, r, f"HTTP Error {r.status_code}: {r.reason_phrase}"

        return url, r, None

    seen_urls = defaultdict(set)
    for u in urls:
        seen_urls[u].add("(command line)")

    running_tasks = set(asyncio.create_task(make_request(u)) for u in seen_urls)
    errors: typing.Dict[str, str] = {}

    while running_tasks:
        finished_tasks, running_tasks = await asyncio.wait(running_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in finished_tasks:
            req_url, r, e = await task
            if e:
                errors[req_url] = str(e)
            elif r.headers['Content-Type'].startswith("text/html") and urlsplit(req_url).netloc in recurse_in_domains:
                for url in extract_links(r.content, str(r.url)):
                    if urlsplit(url).scheme == "mailto":
                        continue
                    if url in ignore or any(fnmatch.fnmatchcase(url, p) for p in ignore_glob):
                        continue
                    seen_urls[url].add(str(req_url))
                    if len(seen_urls[url]) == 1:
                        running_tasks.add(asyncio.create_task(make_request(url)))
    seen_urls.default_factory = None
    for url, error in sorted(errors.items()):
        print(f"{url}:")
        print(f"\tfound on:\n" + "\n".join(f"\t- {u}" for u in seen_urls[url]))
        print(f"\t{error}")
    print(f"{len(seen_urls)} URLs checked in total, {len(errors)} errors")
    return not errors


class AsyncFileStream():
    def __init__(self, f: typing.BinaryIO):
        self.f = f

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        for chunk in iter(lambda: self.f.read(2**20), b''):
            yield chunk

    async def aclose(self) -> None:
        self.f.close()


class AsyncStaticFileTransport(httpx.AsyncBaseTransport):
    def __init__(self, directory: str):
        self.directory = directory

    async def handle_async_request(
        self,
        method: bytes,
        url: typing.Tuple[bytes, bytes, typing.Optional[int], bytes],
        headers: typing.List[typing.Tuple[bytes, bytes]],
        stream: httpx.AsyncByteStream,
        extensions: dict,
    ) -> typing.Tuple[
        int, typing.List[typing.Tuple[bytes, bytes]], httpx.AsyncByteStream, dict
    ]:
        if method != b'GET':
            return 400, [], [b""], {}

        path = posixpath.normpath(unquote(url[3].decode("ascii")))

        # don't allow any path seperators that are not /
        for sep in (os.path.sep, os.path.altsep):
            if sep not in (None, '/') and sep in path:
                return 404, [], [b""], {}

        if '/../' in path:
            return 404, [], [b""], {}

        contenttype, _ = mimetypes.guess_type(path)
        if not contenttype:
            contenttype = "application/octet-steam"
        headers = {b"Content-Type": contenttype.encode("ascii")}
        try:
            try:
                f = open(os.path.join(self.directory, path.lstrip('/')), "rb")
            except IsADirectoryError:
                headers[b"Content-Type"] = b"text/html"
                f = open(os.path.join(self.directory, path.lstrip('/'), "index.html"), "rb")

            return 200, list(headers.items()), AsyncFileStream(f), {}
        except FileNotFoundError:
            return 404, [], [b""], {}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mount', nargs=2, action="append", default=[])
    parser.add_argument('--recurse-in-domain', action="append", default=[])
    parser.add_argument('--user-agent', default=USER_AGENT)
    parser.add_argument('--ignore', action="append", default=[])
    parser.add_argument('--ignore-glob', action="append", default=[])
    parser.add_argument('--ignore-http-code', action="append", default=[])
    parser.add_argument('--disable-certificate-verification', action="append", default=[])
    parser.add_argument('urls', nargs='*', default=[])

    args = parser.parse_args()
    mounts = {"all://": httpx.AsyncHTTPTransport(http2=True)}

    for hostname in args.disable_certificate_verification:
        mounts[f"https://{hostname}"] = httpx.AsyncHTTPTransport(verify=False)

    for directory, url in args.mount:
        mounts[url] = AsyncStaticFileTransport(directory=directory)

    async with httpx.AsyncClient(mounts=mounts) as client:
        return await check_urls(
            client=client, urls=args.urls, recurse_in_domains=set(args.recurse_in_domain),
            user_agent=args.user_agent, ignore=set(args.ignore), ignore_glob=set(args.ignore_glob),
            ignore_http_codes=set(int(c) for c in args.ignore_http_code))


if __name__ == '__main__':
    if asyncio.run(main()):
        sys.exit(0)
    else:
        sys.exit(1)
