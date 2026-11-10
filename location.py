import ipaddress
import json
import os
import re

from ip2region import searcher, util


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


IP2REGION_DIR = os.path.join(BASE_DIR, "data")


UNKNOWN = "未知地域"


PROVINCE_SUFFIX_RE = re.compile(r"(省|市|特别行政区)$")


with open(os.path.join(BASE_DIR, "country_zh.json"), encoding="utf-8") as f:
    COUNTRY_NAMES = json.load(f)


def open_searcher(version, filename):
    content = util.load_content_from_file(os.path.join(IP2REGION_DIR, filename))
    return searcher.new_with_buffer(version, content)


SEARCHERS = {
    4: open_searcher(util.IPv4, "ip2region_v4.xdb"),
    6: open_searcher(util.IPv6, "ip2region_v6.xdb"),
}


def format_region(region):
    _country, province, _city, _isp, code = region.split("|")
    if code == "CN":
        return "中国" if province == "0" else PROVINCE_SUFFIX_RE.sub("", province)
    return COUNTRY_NAMES.get(code, UNKNOWN)


def get_ip_location(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return UNKNOWN
    if not addr.is_global:
        return UNKNOWN
    return format_region(SEARCHERS[addr.version].search(str(addr)))
