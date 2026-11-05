"""Bounded HTTPS download with DNS pinning; no ambient proxies or credentials."""
import asyncio
import ipaddress
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit,urljoin
import aiohttp

class MediaError(ValueError):pass

def validate_url(url,hosts):
    if len(url)>2048:raise MediaError('URL过长')
    p=urlsplit(url)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):raise MediaError('仅支持无凭据的HTTPS默认端口')
    host=p.hostname.encode('idna').decode().lower().rstrip('.')
    if host not in hosts:raise MediaError('来源域未批准')
    return host

class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self,host,port=0,family=socket.AF_INET):
        rows=await asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM)
        result=[]
        for fam,_,_,_,addr in rows:
            ip=ipaddress.ip_address(addr[0]);ip=getattr(ip,'ipv4_mapped',None) or ip
            if not ip.is_global:raise MediaError('禁止访问非公网地址')
            result.append(dict(hostname=host,host=addr[0],port=port,family=fam,proto=0,flags=socket.AI_NUMERICHOST))
        if not result:raise MediaError('DNS未返回公网地址')
        return result
    async def close(self):pass

async def download(url,hosts,max_bytes=8*1024*1024,before_request=None):
    async with asyncio.timeout(20):
        connector=aiohttp.TCPConnector(resolver=PublicResolver(),use_dns_cache=False)
        async with aiohttp.ClientSession(connector=connector,trust_env=False,auto_decompress=False,cookie_jar=aiohttp.DummyCookieJar()) as session:
            for hop in range(4):
                host=validate_url(url,hosts)
                if before_request is not None:await before_request(url)
                # IP literal bypasses aiohttp resolver; reject it here too.
                try:ip=ipaddress.ip_address(host)
                except ValueError:ip=None
                if ip is not None and not ip.is_global:raise MediaError('禁止访问非公网地址')
                now=time.monotonic()
                if _paused.get(host,0)>now:raise MediaError('来源处于限流退避期')
                due=max(now,_next_request.get(host,now));_next_request[host]=due+0.5
                await asyncio.sleep(max(0,due-now))
                async with session.get(url,allow_redirects=False,headers={'Accept-Encoding':'identity','User-Agent':'QQBotAgent-Media/1.0'}) as resp:
                    if resp.status in (301,302,303,307,308):
                        if hop==3:raise MediaError('重定向过多')
                        url=urljoin(url,resp.headers.get('Location',''));continue
                    if resp.status in (429,567):_paused[host]=time.monotonic()+60
                    if resp.status!=200:raise MediaError('来源返回错误或限流')
                    if resp.headers.get('Content-Encoding','identity')!='identity':raise MediaError('不接受压缩传输')
                    if resp.content_length and resp.content_length>max_bytes:raise MediaError('响应超限')
                    data=bytearray()
                    async for chunk in resp.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data)>max_bytes:raise MediaError('响应超限')
                    return bytes(data),resp.headers.get('Content-Type','').split(';')[0],url
    raise MediaError('下载失败')

class Candidates(HTMLParser):
    def __init__(self,base):super().__init__();self.base=base;self.urls=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs);values=[]
        if tag=='meta' and a.get('property')=='og:image':values.append(a.get('content',''))
        if tag=='img':values.extend([a.get('src',''),a.get('data-src',''),a.get('data-original','')])
        if tag in ('source','img'):
            values.extend(x.strip().split(' ')[0] for x in a.get('srcset','').split(',') if x.strip())
        for value in values:
            url=urljoin(self.base,value)
            if value and url not in self.urls and len(self.urls)<6:self.urls.append(url)

_next_request={}
_paused={}
