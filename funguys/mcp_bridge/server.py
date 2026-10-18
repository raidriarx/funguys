"""Bounded public HTTPS document retrieval with pinned, validated DNS addresses."""
from __future__ import annotations
import asyncio
import http.client
import ipaddress
import re
import socket
import ssl
import time
import urllib.parse
from bs4 import BeautifulSoup

_FETCH_LIMIT = asyncio.Semaphore(4)
MAX_RESPONSE = 2_000_000

def public_destination(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('Only public HTTPS URLs on port 443 are accepted')
    addresses = sorted({row[4][0] for row in socket.getaddrinfo(parsed.hostname,443,type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in addresses):
        raise ValueError('Local, private and reserved network destinations are blocked')
    return parsed, addresses[0]

class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, address):
        super().__init__(hostname,443,timeout=10,context=ssl.create_default_context())
        self.address = address
    def connect(self):
        sock = socket.create_connection((self.address,443),timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock,server_hostname=self.host)
        except Exception:
            sock.close()
            raise

def fetch_public_page(url):
    deadline = time.monotonic()+45
    for _ in range(5):
        if time.monotonic()>deadline:
            raise ValueError('Document request exceeded its time limit')
        parsed, address = public_destination(url)
        connection = PinnedHTTPSConnection(parsed.hostname,address)
        try:
            path = urllib.parse.urlunsplit(('', '', parsed.path or '/',parsed.query,''))
            connection.request('GET',path,headers={'User-Agent':'FunGuys/0.2 documentation reader','Accept-Encoding':'identity'})
            response = connection.getresponse()
            if response.status in (301,302,303,307,308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Upstream redirect has no destination')
                url = urllib.parse.urljoin(url,location)
                continue
            if response.status != 200:
                raise ValueError(f'Upstream returned HTTP {response.status}')
            if response.getheader('Content-Encoding','identity').lower() != 'identity':
                raise ValueError('Compressed upstream responses are not accepted')
            content_type = response.getheader('Content-Type','text/html').split(';')[0].lower()
            if not (content_type.startswith('text/') or content_type in ('application/json','application/xhtml+xml')):
                raise ValueError('The requested resource is not a text document')
            chunks,size = [],0
            while size<=MAX_RESPONSE:
                if time.monotonic()>deadline:
                    raise ValueError('Document download exceeded its time limit')
                chunk = response.read1(min(65536,MAX_RESPONSE+1-size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b''.join(chunks)
            if len(raw)>MAX_RESPONSE:
                raise ValueError('Upstream document exceeds the 2 MB limit')
            return raw.decode('utf-8',errors='replace')
        finally:
            connection.close()
    raise ValueError('Too many upstream redirects')

async def fetch_docs_markdown(url,max_chars=8000):
    async with _FETCH_LIMIT:
        html = await asyncio.wait_for(asyncio.to_thread(fetch_public_page,url),timeout=55)
    soup = BeautifulSoup(html,'html.parser')
    for tag in soup(['script','style','nav','footer','noscript']):
        tag.decompose()
    return re.sub(r'\n{3,}','\n\n',soup.get_text(separator='\n')).strip()[:max_chars]

async def web_search(query,max_results=5):
    url = 'https://html.duckduckgo.com/html/?q=' + urllib.parse.quote_plus(query)
    async with _FETCH_LIMIT:
        html = await asyncio.wait_for(asyncio.to_thread(fetch_public_page,url),timeout=55)
    soup = BeautifulSoup(html,'html.parser')
    results = []
    for item in soup.select('.result')[:max(1,min(max_results,10))]:
        title = item.select_one('.result__title a')
        snippet = item.select_one('.result__snippet')
        if title:
            link = str(title.get('href',''))
            destination = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get('uddg',[link])[0]
            if urllib.parse.urlsplit(destination).scheme not in ('http','https'):
                continue
            results.append({'title':title.get_text(strip=True),'url':destination,'snippet':snippet.get_text(strip=True) if snippet else ''})
    return results
