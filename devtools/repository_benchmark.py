"""Independently grade a historical bug repair in a pinned upstream repository.

Opt-in, operator-run evaluation. Never run against a developer's live checkout.
The visible test is in the disposable source tree; the hidden oracle is not.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
import tomllib

from ash.commands.chatgpt_auth import list_chatgpt_models
from ash.core.activity import EDIT_TOOL_NAMES, summarize_operational_events
from ash.core.session import SessionStore, ToolCallRecord
from ash.exceptions import ErrorCategory
from ash.providers.openai_chatgpt_auth import ChatGPTAccount, ChatGPTCredentialStore

from coding_benchmark import (
    CHATGPT_TURN_ITERATIONS,
    PYTHON,
    REPO,
    _chatgpt_tmpfs_root,
    _stage_chatgpt_access_only,
    command,
    oracle_python,
)
from evaluation_files import (
    UNSAFE_ENTRIES,
    VERIFICATION_ORDER_VERSION,
    content_identity,
    snapshot_workspace_files as snapshot_files,
    verification_freshness,
)


H11_VISIBLE = """from h11._events import Data, EndOfMessage
from h11._readers import ChunkedReader
from h11._receivebuffer import ReceiveBuffer
from h11._util import LocalProtocolError

def events_for(chunks):
    reader, buffer, events = ChunkedReader(), ReceiveBuffer(), []
    for chunk in chunks:
        buffer += chunk
        while True:
            event = reader(buffer)
            if event is None:
                break
            events.append(event)
            if isinstance(event, EndOfMessage):
                return events
    return events

good = events_for([b"3\\r\\nabc\\r\\n0\\r\\n\\r\\n"])
assert [type(event) for event in good] == [Data, EndOfMessage]
assert good[0].data == b"abc"

for malformed in [b"3\\r\\nabc__0\\r\\n\\r\\n", b"3\\r\\nabc\\r_0\\r\\n\\r\\n"]:
    try:
        events_for([malformed])
    except LocalProtocolError:
        pass
    else:
        raise AssertionError("chunk footer accepted without mandatory CRLF")
"""

H11_HIDDEN = """from h11._events import Data, EndOfMessage
from h11._readers import ChunkedReader
from h11._receivebuffer import ReceiveBuffer
from h11._util import LocalProtocolError

def parse(fragments):
    reader, buffer, events = ChunkedReader(), ReceiveBuffer(), []
    for fragment in fragments:
        buffer += fragment
        while True:
            event = reader(buffer)
            if event is None:
                break
            events.append(event)
            if isinstance(event, EndOfMessage):
                return events
    return events

for bad in (b"3\\r\\nabc_\\n0\\r\\n\\r\\n", b"3\\r\\nabc\\r_0\\r\\n\\r\\n", b"3\\r\\nabc__0\\r\\n\\r\\n"):
    for fragments in ([bad], [bytes([byte]) for byte in bad], [bad[:6], bad[6:8], bad[8:]]):
        try:
            parse(fragments)
        except LocalProtocolError:
            pass
        else:
            raise AssertionError("bad chunk terminator was accepted")

good = b"3\\r\\nabc\\r\\n0\\r\\n\\r\\n"
for fragments in ([good], [bytes([byte]) for byte in good], [good[:6], good[6:8], good[8:]]):
    events = parse(fragments)
    assert events and isinstance(events[-1], EndOfMessage)
    assert b"".join(e.data for e in events if isinstance(e, Data)) == b"abc"
"""

URLLIB3_VISIBLE = r"""import sys
import types
# The upstream source checkout omits the generated build-time version module.
version = types.ModuleType("urllib3._version")
version.__version__ = "0.0+repository-benchmark"
sys.modules["urllib3._version"] = version
sys.path.insert(0, "src")
from urllib3.connection import _normalize_header_value

examples = [
    ("basic", "basic"),
    ("one\r\n two", "one two"),
    ("value\ninjected", "value injected"),
    ("value\rinjected", "value injected"),
    ("value\x00injected", "value injected"),
    (" \tvalue\t ", "value"),
]
for raw, expected in examples:
    actual = _normalize_header_value(raw)
    assert actual == expected, (repr(raw), repr(actual), repr(expected))
"""

URLLIB3_HIDDEN = r"""import sys
import types
version = types.ModuleType("urllib3._version")
version.__version__ = "0.0+repository-benchmark"
sys.modules["urllib3._version"] = version
sys.path.insert(0, "src")
from urllib3.connection import _normalize_header_value

examples = [
    ("one\r\n two\t\r\n\tthree", "one two three"),
    ("one\r\n \r\n\tthree", "one  three"),
    ("one\n two", "one  two"),
    ("one\r two", "one  two"),
    ("one\r\n\ttwo\n three", "one two  three"),
    ("value\r\n ", "value"),
    ("value\x00", "value"),
    (" \t", ""),
    ("\r\n\x00", ""),
]
for raw, expected in examples:
    actual = _normalize_header_value(raw)
    assert actual == expected, (repr(raw), repr(actual), repr(expected))
    assert "\r" not in actual and "\n" not in actual and "\x00" not in actual
assert _normalize_header_value("interior \t whitespace") == "interior \t whitespace"
"""

URLLIB3_REDIRECT_VISIBLE = r"""import sys
import types
version = types.ModuleType("urllib3._version")
version.__version__ = "0.0+repository-benchmark"
sys.modules["urllib3._version"] = version
sys.path.insert(0, "src")
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.poolmanager import ProxyManager
from urllib3.util.retry import Retry

class Response:
    status = 302
    retries = Retry(total=2, redirect=2)
    def __init__(self, location=None):
        self.location = location
        if location is None:
            self.status = 200
    def get_redirect_location(self):
        return self.location or False
    def drain_conn(self):
        pass

class RecordingProxyPool:
    def __init__(self, redirect):
        self.redirect = redirect
        self.proxy_pool = HTTPConnectionPool("proxy.internal", 3128)
        self.requests = []
    def is_same_host(self, url):
        return self.proxy_pool.is_same_host(url)
    def urlopen(self, method, url, **kw):
        self.requests.append((url, dict(kw["headers"])))
        return Response(self.redirect if len(self.requests) == 1 else None)

def exercise(first_url, redirect):
    pool = RecordingProxyPool(redirect)
    manager = ProxyManager("http://proxy.internal:3128")
    manager.connection_from_host = lambda *args, **kw: pool
    response = manager.urlopen(
        "GET", first_url, headers={"Authorization": "secret", "Cookie": "id=123"},
        retries=Retry(total=2, redirect=2),
    )
    assert response.status == 200
    assert len(pool.requests) == 2
    return pool.requests

same = exercise("http://alpha.invalid/start", "/next")
assert same[1][1]["Authorization"] == "secret"
assert same[1][1]["Cookie"] == "id=123"
assert same[1][1]["Host"] == "alpha.invalid"

cross = exercise("http://alpha.invalid/start", "http://beta.invalid/next")
assert "Authorization" not in cross[1][1]
assert "Cookie" not in cross[1][1]
assert cross[1][1]["Host"] == "beta.invalid"
"""

URLLIB3_REDIRECT_HIDDEN = r"""import sys
import types
version = types.ModuleType("urllib3._version")
version.__version__ = "0.0+repository-benchmark"
sys.modules["urllib3._version"] = version
sys.path.insert(0, "src")
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.poolmanager import ProxyManager
from urllib3.util.retry import Retry

class Response:
    retries = Retry(total=2, redirect=2)
    def __init__(self, location=None):
        self.location = location
        self.status = 302 if location else 200
    def get_redirect_location(self):
        return self.location or False
    def drain_conn(self):
        pass

class RecordingProxyPool:
    def __init__(self, redirect):
        self.proxy_pool = HTTPConnectionPool("proxy.internal", 3128)
        self.redirect = redirect
        self.requests = []
    def is_same_host(self, url):
        return self.proxy_pool.is_same_host(url)
    def urlopen(self, method, url, **kw):
        self.requests.append((url, dict(kw["headers"])))
        return Response(self.redirect if len(self.requests) == 1 else None)

def exercise(first_url, location, headers):
    pool = RecordingProxyPool(location)
    manager = ProxyManager("http://proxy.internal:3128")
    manager.connection_from_host = lambda *args, **kw: pool
    manager.urlopen(
        "GET", first_url, headers=headers, retries=Retry(total=2, redirect=2)
    )
    assert len(pool.requests) == 2
    return pool.requests[1][1]

case = exercise(
    "http://alpha.invalid:80/start", "http://alpha.invalid/next",
    {"Authorization": "keep", "Cookie": "id=keep", "Host": "alpha.invalid:80"},
)
assert case["Authorization"] == "keep"
assert case["Cookie"] == "id=keep"
assert case["Host"] == "alpha.invalid:80"

case = exercise(
    "http://alpha.invalid/start", "http://alpha.invalid:8080/next",
    {"Authorization": "remove", "Cookie": "id=remove", "Host": "alpha.invalid"},
)
assert "Authorization" not in case
assert "Cookie" not in case
assert case["Host"] == "alpha.invalid:8080"

case = exercise(
    "http://alpha.invalid/start", "/same",
    {"Authorization": "keep", "Cookie": "id=keep", "Host": "alpha.invalid"},
)
assert case["Authorization"] == "keep"
assert case["Cookie"] == "id=keep"
assert case["Host"] == "alpha.invalid"
"""

URLLIB3_AUTH_FEATURE_VISIBLE = r"""import sys
import types
version = types.ModuleType("urllib3._version")
version.__version__ = "0.0+repository-benchmark"
sys.modules["urllib3._version"] = version
sys.path.insert(0, "src")
from urllib3.util.url import parse_url

examples = [
    ("http://someone:secret@example.invalid/", ("someone", "secret"), "someone:secret"),
    ("http://someone@example.invalid/", ("someone", None), "someone:"),
    ("http://someone:@example.invalid/", ("someone", ""), "someone:"),
    ("http://user%40mail:pass%3Aword@example.invalid/", ("user@mail", "pass:word"), "user@mail:pass:word"),
    ("http://example.invalid/", (None, None), None),
]
for uri, pair, joined in examples:
    result = parse_url(uri)
    assert result.auth_decoded == pair, (uri, result.auth_decoded)
    assert result.auth_decoded_joined == joined, (uri, result.auth_decoded_joined)
    assert result.auth == (uri.split("@", 1)[0].split("//", 1)[1] if "@" in uri else None)
"""

URLLIB3_AUTH_FEATURE_HIDDEN = r"""import sys
import types
version = types.ModuleType("urllib3._version")
version.__version__ = "0.0+repository-benchmark"
sys.modules["urllib3._version"] = version
sys.path.insert(0, "src")
from urllib3.util.url import parse_url, Url

examples = [
    ("http://name%3Awith%3Acolon:p%40ss%2Fword@example.invalid/", ("name:with:colon", "p@ss/word"), "name:with:colon:p@ss/word"),
    ("http://%E6%BC%A2%E5%AD%97:%C3%A9@example.invalid/", ("漢字", "é"), "漢字:é"),
    ("http://user-name:pass_word@example.invalid/", ("user-name", "pass_word"), "user-name:pass_word"),
    ("http://x%20y@example.invalid/", ("x y", None), "x y:"),
    ("http://user:pass:more@example.invalid/", ("user", "pass:more"), "user:pass:more"),
    ("http://:pass@example.invalid/", ("", "pass"), ":pass"),
    ("http://example.invalid/", (None, None), None),
]
for uri, pair, joined in examples:
    result = parse_url(uri)
    assert result.auth_decoded == pair, (uri, result.auth_decoded)
    assert result.auth_decoded_joined == joined, (uri, result.auth_decoded_joined)

raw = Url(scheme="http", auth="first%3Aname:second%3Avalue", host="example.invalid")
assert raw.auth_decoded == ("first:name", "second:value")
assert raw.auth_decoded_joined == "first:name:second:value"
assert raw.auth == "first%3Aname:second%3Avalue"
"""

CLICK_RESOURCE_VISIBLE = r"""import sys
sys.path.insert(0, "src")
import click

class RollbackRequested(Exception):
    pass

class ManagedTransaction:
    def __init__(self, suppress=False):
        self.suppress = suppress
        self.seen = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.seen.append((exc_type, exc_value, traceback is not None))
        return self.suppress if exc_type is not None else False

# Ordinary successful cleanup stays unchanged.
ctx = click.Context(click.Command("transaction"))
success = ManagedTransaction()
with ctx:
    assert ctx.with_resource(success) is success
assert success.seen == [(None, None, False)]

# Unhandled exceptions must reach managed resources, not commit as success.
failure = ManagedTransaction()
reason = RollbackRequested("rollback the operation")
try:
    with ctx:
        ctx.with_resource(failure)
        raise reason
except RollbackRequested as actual:
    assert actual is reason
else:
    raise AssertionError("exception unexpectedly swallowed")
assert failure.seen == [(RollbackRequested, reason, True)]

# A resource which explicitly handles an exception may suppress it.
handled = ManagedTransaction(suppress=True)
with ctx:
    ctx.with_resource(handled)
    raise RollbackRequested("handled inside the resource")
assert handled.seen[0][0] is RollbackRequested
assert handled.seen[0][2] is True
"""

CLICK_RESOURCE_HIDDEN = r"""import sys
sys.path.insert(0, "src")
import click

class AbortTransaction(Exception):
    pass

events = []

class Resource:
    def __init__(self, label, swallow=False):
        self.label = label
        self.swallow = swallow

    def __enter__(self):
        events.append((self.label, "entered"))
        return self

    def __exit__(self, typ, value, traceback):
        events.append((self.label, typ, value, traceback))
        return bool(typ is not None and self.swallow)

ctx = click.Context(click.Command("audit"))

# ExitStack unwinds LIFO; a swallowed error must not reach outer resources.
reason = AbortTransaction("inner handled the error")
with ctx.scope():
    ctx.with_resource(Resource("outer"))
    ctx.with_resource(Resource("inner", swallow=True))
    raise reason
assert events[0:2] == [("outer", "entered"), ("inner", "entered")]
assert events[2][0] == "inner" and events[2][1] is AbortTransaction
assert events[2][2] is reason and events[2][3] is not None
assert events[3] == ("outer", None, None, None), events

# With no suppressor, all resources receive the same live exception.
events.clear()
reason = AbortTransaction("propagate through both")
try:
    with ctx.scope():
        ctx.with_resource(Resource("outer"))
        ctx.with_resource(Resource("inner"))
        raise reason
except AbortTransaction as actual:
    assert actual is reason
else:
    raise AssertionError("resource incorrectly swallowed exception")
assert [entry[0] for entry in events] == ["outer", "inner", "inner", "outer"]
for entry in events[2:]:
    assert entry[1] is AbortTransaction and entry[2] is reason
    assert entry[3] is not None

# Reusing the context resets its cleanup stack, without stale callbacks.
events.clear()
with ctx:
    ctx.with_resource(Resource("first"))
with ctx:
    ctx.with_resource(Resource("second"))
assert [event[0] for event in events] == ["first", "first", "second", "second"]
assert all(entry[1:] == (None, None, None) for entry in (events[1], events[3]))
"""

DATEUTIL_NUMERIC_VISIBLE = r"""import sys
from io import StringIO
from datetime import datetime, timedelta
sys.path.insert(0, "src")
from dateutil.parser import isoparse
from dateutil.tz import tzical

valid = isoparse("2020-02-29T23:45:10+05:30")
assert valid == datetime(2020, 2, 29, 23, 45, 10, tzinfo=valid.tzinfo)
assert valid.utcoffset() == timedelta(hours=5, minutes=30)
assert isoparse("2024-W01-1").date().isoformat() == "2024-01-01"

# Fixed-width ISO date/time fields must not accept int()-compatible garbage.
for malformed in (
    "2_14-04-21",
    "2014- 4-21",
    "2014-04-21T14:3",
    "2014-04-21T14:30+-1:00",
    "20200728121240",
):
    try:
        isoparse(malformed)
    except ValueError:
        pass
    else:
        raise AssertionError("malformed ISO field accepted: " + repr(malformed))

def zone(offset):
    source = (
        "BEGIN:VCALENDAR\nBEGIN:VTIMEZONE\nTZID:Example\n"
        "BEGIN:STANDARD\nDTSTART:19700101T000000\nTZOFFSETFROM:+0000\n"
        "TZOFFSETTO:" + offset + "\nTZNAME:Example\n"
        "END:STANDARD\nEND:VTIMEZONE\nEND:VCALENDAR\n"
    )
    return tzical(StringIO(source)).get()

assert zone("-0500") is not None
assert zone("-053000") is not None
for malformed in ("+-500", "+ 500"):
    try:
        zone(malformed)
    except ValueError:
        pass
    else:
        raise AssertionError("malformed TZOFFSETTO accepted: " + repr(malformed))
"""

DATEUTIL_NUMERIC_HIDDEN = r"""import sys
from io import StringIO
from datetime import datetime, timedelta
sys.path.insert(0, "src")
from dateutil.parser import isoparse, isoparser
from dateutil.tz import tzical

# Exercise other numeric fields than the visible oracle, including offset
# minutes, ordinal/week forms, and a truncated final component.
for malformed in (
    "2014-W1 -1",
    "2014-04-21T14:30:1",
    "2014-04-21T14:30+01:-5",
    "2014-04-21T14:30+01000",
    "2014-04-21T12: 5",
    "2014-04-21T14:30+01:0",
):
    try:
        isoparse(malformed)
    except ValueError:
        pass
    else:
        raise AssertionError("non-digit or truncated ISO field accepted: " + repr(malformed))

assert isoparser().parse_isodate("2024-366").isoformat() == "2024-12-31"
assert isoparser().parse_isotime("14:30:25").isoformat() == "14:30:25"
parsed = isoparse("2020-01-02T10:30:15-04:00")
assert parsed == datetime(2020, 1, 2, 10, 30, 15, tzinfo=parsed.tzinfo)
assert parsed.utcoffset() == -timedelta(hours=4)

def zone(from_offset, to_offset):
    source = (
        "BEGIN:VCALENDAR\nBEGIN:VTIMEZONE\nTZID:Independent\n"
        "BEGIN:STANDARD\nDTSTART:19700101T000000\n"
        "TZOFFSETFROM:" + from_offset + "\n"
        "TZOFFSETTO:" + to_offset + "\nTZNAME:Independent\n"
        "END:STANDARD\nEND:VTIMEZONE\nEND:VCALENDAR\n"
    )
    return tzical(StringIO(source)).get()

assert zone("+0000", "-0500") is not None
for malformed_from, malformed_to in (
    ("--500", "-0500"),
    ("+0000", "-05 0"),
    ("+٠٥٠٠", "-0500"),
):
    try:
        zone(malformed_from, malformed_to)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid calendar timezone offset accepted")
"""


ATTRS_GENERATOR_VISIBLE = r"""import sys
sys.path.insert(0, 'src')
import attr
import attrs

events = []

def before_after(obj, attribute, value):
    events.append(('before', attribute.name, getattr(obj, attribute.name), value))
    yield value * 2
    events.append(('after', attribute.name, getattr(obj, attribute.name)))

@attrs.define(on_setattr=before_after)
class Counter:
    amount: int = 3

counter = Counter()
events.clear()
counter.amount = 5
assert counter.amount == 10
assert events == [('before', 'amount', 3, 5), ('after', 'amount', 10)]
counter.amount = 7
assert counter.amount == 14

def add_three(obj, attribute, value):
    return value + 3

@attrs.define(on_setattr=add_three)
class Hybrid:
    plain: int = 1
    hooked: int = attrs.field(default=2, on_setattr=before_after)

hybrid = Hybrid()
hybrid.plain = 4
assert hybrid.plain == 7
hybrid.hooked = 6
assert hybrid.hooked == 12

@attr.s(auto_attribs=True, on_setattr=before_after)
class Legacy:
    amount: int = 2

legacy = Legacy()
legacy.amount = 8
assert legacy.amount == 16
"""


ATTRS_GENERATOR_HIDDEN = r"""import sys
sys.path.insert(0, 'src')
import attrs
from attrs import setters

trace = []

def hook(obj, attribute, value):
    trace.append(('enter', attribute.name, value))
    try:
        yield value + 10
        trace.append(('exit', getattr(obj, attribute.name)))
    finally:
        trace.append(('closed', attribute.name))

@attrs.define(on_setattr=hook)
class Item:
    value: int = 1
    untouched: int = attrs.field(default=2, on_setattr=setters.NO_OP)

item = Item()
trace.clear()
item.value = 3
assert item.value == 13
assert trace == [('enter', 'value', 3), ('exit', 13), ('closed', 'value')]
item.untouched = 5
assert item.untouched == 5
assert len(trace) == 3

def empty_generator(obj, attribute, value):
    if False:
        yield value

@attrs.define(on_setattr=empty_generator)
class Empty:
    value: int = 2

empty = Empty()
try:
    empty.value = 9
except StopIteration:
    pass
else:
    raise AssertionError('zero-yield generator hook unexpectedly accepted')
assert empty.value == 2

closed = []
def too_many(obj, attribute, value):
    try:
        yield value * 3
        yield value * 4
    finally:
        closed.append(True)

@attrs.define(on_setattr=too_many)
class Extra:
    value: int = 2

extra = Extra()
try:
    extra.value = 4
except RuntimeError as exc:
    assert 'yielded more than once' in str(exc)
else:
    raise AssertionError('multiple-yield hook unexpectedly accepted')
assert extra.value == 12
assert closed == [True]

def usual(obj, attribute, value):
    return value - 1

@attrs.define(on_setattr=usual)
class Ordinary:
    number: int = 4

ordinary = Ordinary()
ordinary.number = 10
assert ordinary.number == 9
"""


PACKAGING_LEGACY_DATACLASS_VISIBLE = r'''"""Frozen original-format packaging 26.3 compatibility test. No network."""
from __future__ import annotations
import base64
import dataclasses
import importlib
import pickle
import sys
from collections.abc import Mapping
sys.path.insert(0,'src')

def normalize(v):
    if dataclasses.is_dataclass(v) and not isinstance(v,type):
        return {'class':f'{type(v).__module__}.{type(v).__qualname__}',
                'fields':{f.name:normalize(getattr(v,f.name)) for f in dataclasses.fields(v)}}
    if isinstance(v,Mapping):
        return {'map':{str(k):normalize(val) for k,val in sorted(v.items(),key=lambda pair:str(pair[0]))}}
    if isinstance(v,(list,tuple)):
        return {'sequence':[normalize(x) for x in v]}
    if v is None or type(v) in (str,int,float,bool):
        return v
    if type(v).__module__.startswith('packaging.'):
        return {'class':f'{type(v).__module__}.{type(v).__qualname__}', 'string':str(v)}
    raise TypeError(f'unsupported state value {type(v)}')

def check_payload(name,row):
    module_name,class_name=row['class'].rsplit('.',1)
    cls=getattr(importlib.import_module(module_name),class_name)
    value=pickle.loads(base64.b64decode(row['blob'],validate=True))
    assert type(value) is cls,(name,type(value),cls)
    observed=normalize(value)
    assert observed==row['expected'],(name,observed,row['expected'])
    return value

FIXTURES={'visible_direct_url_nested_vcs': {'class': 'packaging.direct_url.DirectUrl', 'blob': 'gASV4wAAAAAAAACMFHBhY2thZ2luZy5kaXJlY3RfdXJslIwJRGlyZWN0VXJslJOUKYGUfZQojAN1cmyUjBhodHRwczovL2V4YW1wbGUub3JnL3JlcG+UjAxhcmNoaXZlX2luZm+UTowIdmNzX2luZm+UaACMB1Zjc0luZm+Uk5QpgZR9lCiMA3Zjc5SMA2dpdJSMCWNvbW1pdF9pZJSMCGNhZmViYWJllIwScmVxdWVzdGVkX3JldmlzaW9ulIwHcmVsZWFzZZR1YowIZGlyX2luZm+UTowMc3ViZGlyZWN0b3J5lIwDbGlilHViLg==', 'expected': {'class': 'packaging.direct_url.DirectUrl', 'fields': {'url': 'https://example.org/repo', 'archive_info': None, 'vcs_info': {'class': 'packaging.direct_url.VcsInfo', 'fields': {'vcs': 'git', 'commit_id': 'cafebabe', 'requested_revision': 'release'}}, 'dir_info': None, 'subdirectory': 'lib'}}}, 'visible_package_vcs': {'class': 'packaging.pylock.PackageVcs', 'blob': 'gASVrwAAAAAAAACMEHBhY2thZ2luZy5weWxvY2uUjApQYWNrYWdlVmNzlJOUKYGUfZQojAR0eXBllIwDZ2l0lIwDdXJslIwcaHR0cHM6Ly9leGFtcGxlLm9yZy9yZXBvLmdpdJSMBHBhdGiUTowScmVxdWVzdGVkX3JldmlzaW9ulIwGc3RhYmxllIwJY29tbWl0X2lklIwIMDEyMzQ1NjeUjAxzdWJkaXJlY3RvcnmUjANwa2eUdWIu', 'expected': {'class': 'packaging.pylock.PackageVcs', 'fields': {'type': 'git', 'url': 'https://example.org/repo.git', 'path': None, 'requested_revision': 'stable', 'commit_id': '01234567', 'subdirectory': 'pkg'}}}, 'visible_pylock_nested': {'class': 'packaging.pylock.Pylock', 'blob': 'gASVSQMAAAAAAACMEHBhY2thZ2luZy5weWxvY2uUjAZQeWxvY2uUk5QpgZR9lCiMDGxvY2tfdmVyc2lvbpSMEXBhY2thZ2luZy52ZXJzaW9ulIwHVmVyc2lvbpSTlCmBlChLAEsBSwCGlE5OTk50lGKMDGVudmlyb25tZW50c5ROjA9yZXF1aXJlc19weXRob26UTowGZXh0cmFzlE6MEWRlcGVuZGVuY3lfZ3JvdXBzlE6MDmRlZmF1bHRfZ3JvdXBzlE6MCmNyZWF0ZWRfYnmUjAdhc2gtcjIzlIwIcGFja2FnZXOUXZQoaACMB1BhY2thZ2WUk5QpgZR9lCiMBG5hbWWUjAZ3aWRnZXSUjAd2ZXJzaW9ulGgIKYGUKEsASwFLAEsAh5ROTk5OdJRijAZtYXJrZXKUTmgNTowMZGVwZW5kZW5jaWVzlE6MA3Zjc5ROjAlkaXJlY3RvcnmUTowHYXJjaGl2ZZROjAVpbmRleJROjAVzZGlzdJROjAZ3aGVlbHOUXZRoAIwMUGFja2FnZVdoZWVslJOUKYGUfZQoaBmMHXdpZGdldC0xLjAuMC1weTMtbm9uZS1hbnkud2hslIwLdXBsb2FkX3RpbWWUTowDdXJslE6MBHBhdGiUTowEc2l6ZZRLgIwGaGFzaGVzlH2UjAZzaGEyNTaUjEBkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRklHN1YmGMFmF0dGVzdGF0aW9uX2lkZW50aXRpZXOUTowEdG9vbJROdWJoFimBlH2UKGgZjAp3aWRnZXQtbGlilGgbaAgpgZQoSwBLAksAhpROTk5OdJRiaB9OaA1OaCBOaCFoAIwKUGFja2FnZVZjc5STlCmBlH2UKIwEdHlwZZSMA2dpdJRoLowcaHR0cHM6Ly9leGFtcGxlLm9yZy9yZXBvLmdpdJRoL06MEnJlcXVlc3RlZF9yZXZpc2lvbpSMBnN0YWJsZZSMCWNvbW1pdF9pZJSMCDAxMjM0NTY3lIwMc3ViZGlyZWN0b3J5lIwDcGtnlHViaCJOaCNOaCROaCVOaCZOaDVOaDZOdWJlaDZOdWIu', 'expected': {'class': 'packaging.pylock.Pylock', 'fields': {'lock_version': {'class': 'packaging.version.Version', 'string': '1.0'}, 'environments': None, 'requires_python': None, 'extras': None, 'dependency_groups': None, 'default_groups': None, 'created_by': 'ash-r23', 'packages': {'sequence': [{'class': 'packaging.pylock.Package', 'fields': {'name': 'widget', 'version': {'class': 'packaging.version.Version', 'string': '1.0.0'}, 'marker': None, 'requires_python': None, 'dependencies': None, 'vcs': None, 'directory': None, 'archive': None, 'index': None, 'sdist': None, 'wheels': {'sequence': [{'class': 'packaging.pylock.PackageWheel', 'fields': {'name': 'widget-1.0.0-py3-none-any.whl', 'upload_time': None, 'url': None, 'path': None, 'size': 128, 'hashes': {'map': {'sha256': 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'}}}}]}, 'attestation_identities': None, 'tool': None}}, {'class': 'packaging.pylock.Package', 'fields': {'name': 'widget-lib', 'version': {'class': 'packaging.version.Version', 'string': '2.0'}, 'marker': None, 'requires_python': None, 'dependencies': None, 'vcs': {'class': 'packaging.pylock.PackageVcs', 'fields': {'type': 'git', 'url': 'https://example.org/repo.git', 'path': None, 'requested_revision': 'stable', 'commit_id': '01234567', 'subdirectory': 'pkg'}}, 'directory': None, 'archive': None, 'index': None, 'sdist': None, 'wheels': None, 'attestation_identities': None, 'tool': None}}]}, 'tool': None}}}, 'visible_vcs_info': {'class': 'packaging.direct_url.VcsInfo', 'blob': 'gASVbgAAAAAAAACMFHBhY2thZ2luZy5kaXJlY3RfdXJslIwHVmNzSW5mb5STlCmBlH2UKIwDdmNzlIwDZ2l0lIwJY29tbWl0X2lklIwIY2FmZWJhYmWUjBJyZXF1ZXN0ZWRfcmV2aXNpb26UjAdyZWxlYXNllHViLg==', 'expected': {'class': 'packaging.direct_url.VcsInfo', 'fields': {'vcs': 'git', 'commit_id': 'cafebabe', 'requested_revision': 'release'}}}}

for name,row in FIXTURES.items():
    check_payload(name,row)

print("R23_PACKAGING_VISIBLE_OK")
'''

PACKAGING_LEGACY_DATACLASS_HIDDEN = r'''"""Frozen original-format packaging 26.3 compatibility test. No network."""
from __future__ import annotations
import base64
import dataclasses
import importlib
import pickle
import sys
from collections.abc import Mapping
sys.path.insert(0,'src')

def normalize(v):
    if dataclasses.is_dataclass(v) and not isinstance(v,type):
        return {'class':f'{type(v).__module__}.{type(v).__qualname__}',
                'fields':{f.name:normalize(getattr(v,f.name)) for f in dataclasses.fields(v)}}
    if isinstance(v,Mapping):
        return {'map':{str(k):normalize(val) for k,val in sorted(v.items(),key=lambda pair:str(pair[0]))}}
    if isinstance(v,(list,tuple)):
        return {'sequence':[normalize(x) for x in v]}
    if v is None or type(v) in (str,int,float,bool):
        return v
    if type(v).__module__.startswith('packaging.'):
        return {'class':f'{type(v).__module__}.{type(v).__qualname__}', 'string':str(v)}
    raise TypeError(f'unsupported state value {type(v)}')

def check_payload(name,row):
    module_name,class_name=row['class'].rsplit('.',1)
    cls=getattr(importlib.import_module(module_name),class_name)
    value=pickle.loads(base64.b64decode(row['blob'],validate=True))
    assert type(value) is cls,(name,type(value),cls)
    observed=normalize(value)
    assert observed==row['expected'],(name,observed,row['expected'])
    return value

FIXTURES={'hidden_archive_info': {'class': 'packaging.direct_url.ArchiveInfo', 'blob': 'gASVhwAAAAAAAACMFHBhY2thZ2luZy5kaXJlY3RfdXJslIwLQXJjaGl2ZUluZm+Uk5QpgZR9lIwGaGFzaGVzlH2UjAZzaGEyNTaUjEBhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhlHNzYi4=', 'expected': {'class': 'packaging.direct_url.ArchiveInfo', 'fields': {'hashes': {'map': {'sha256': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}}}}}, 'hidden_dir_info': {'class': 'packaging.direct_url.DirInfo', 'blob': 'gASVNwAAAAAAAACMFHBhY2thZ2luZy5kaXJlY3RfdXJslIwHRGlySW5mb5STlCmBlH2UjAhlZGl0YWJsZZSIc2Iu', 'expected': {'class': 'packaging.direct_url.DirInfo', 'fields': {'editable': True}}}, 'hidden_direct_url_archive': {'class': 'packaging.direct_url.DirectUrl', 'blob': 'gASVAQEAAAAAAACMFHBhY2thZ2luZy5kaXJlY3RfdXJslIwJRGlyZWN0VXJslJOUKYGUfZQojAN1cmyUjCJodHRwczovL2ZpbGVzLmV4YW1wbGUub3JnL2Rpc3Qud2hslIwMYXJjaGl2ZV9pbmZvlGgAjAtBcmNoaXZlSW5mb5STlCmBlH2UjAZoYXNoZXOUfZSMBnNoYTI1NpSMQGFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWGUc3NijAh2Y3NfaW5mb5ROjAhkaXJfaW5mb5ROjAxzdWJkaXJlY3RvcnmUTnViLg==', 'expected': {'class': 'packaging.direct_url.DirectUrl', 'fields': {'url': 'https://files.example.org/dist.whl', 'archive_info': {'class': 'packaging.direct_url.ArchiveInfo', 'fields': {'hashes': {'map': {'sha256': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}}}}, 'vcs_info': None, 'dir_info': None, 'subdirectory': None}}}, 'hidden_direct_url_directory': {'class': 'packaging.direct_url.DirectUrl', 'blob': 'gASVoQAAAAAAAACMFHBhY2thZ2luZy5kaXJlY3RfdXJslIwJRGlyZWN0VXJslJOUKYGUfZQojAN1cmyUjBJmaWxlOi8vL3Nydi9zb3VyY2WUjAxhcmNoaXZlX2luZm+UTowIdmNzX2luZm+UTowIZGlyX2luZm+UaACMB0RpckluZm+Uk5QpgZR9lIwIZWRpdGFibGWUiHNijAxzdWJkaXJlY3RvcnmUTnViLg==', 'expected': {'class': 'packaging.direct_url.DirectUrl', 'fields': {'url': 'file:///srv/source', 'archive_info': None, 'vcs_info': None, 'dir_info': {'class': 'packaging.direct_url.DirInfo', 'fields': {'editable': True}}, 'subdirectory': None}}}, 'hidden_package_archive': {'class': 'packaging.pylock.PackageArchive', 'blob': 'gASV2QAAAAAAAACMEHBhY2thZ2luZy5weWxvY2uUjA5QYWNrYWdlQXJjaGl2ZZSTlCmBlH2UKIwDdXJslIwZaHR0cHM6Ly9leGFtcGxlLm9yZy9hLndobJSMBHBhdGiUTowEc2l6ZZRLlIwLdXBsb2FkX3RpbWWUTowGaGFzaGVzlH2UjAZzaGEyNTaUjEBiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJilHOMDHN1YmRpcmVjdG9yeZROdWIu', 'expected': {'class': 'packaging.pylock.PackageArchive', 'fields': {'url': 'https://example.org/a.whl', 'path': None, 'size': 148, 'upload_time': None, 'hashes': {'map': {'sha256': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'}}, 'subdirectory': None}}}, 'hidden_package_direct': {'class': 'packaging.pylock.Package', 'blob': 'gASVnwEAAAAAAACMEHBhY2thZ2luZy5weWxvY2uUjAdQYWNrYWdllJOUKYGUfZQojARuYW1llIwKd2lkZ2V0LWxpYpSMB3ZlcnNpb26UjBFwYWNrYWdpbmcudmVyc2lvbpSMB1ZlcnNpb26Uk5QpgZQoSwBLAksAhpROTk5OdJRijAZtYXJrZXKUTowPcmVxdWlyZXNfcHl0aG9ulE6MDGRlcGVuZGVuY2llc5ROjAN2Y3OUaACMClBhY2thZ2VWY3OUk5QpgZR9lCiMBHR5cGWUjANnaXSUjAN1cmyUjBxodHRwczovL2V4YW1wbGUub3JnL3JlcG8uZ2l0lIwEcGF0aJROjBJyZXF1ZXN0ZWRfcmV2aXNpb26UjAZzdGFibGWUjAljb21taXRfaWSUjAgwMTIzNDU2N5SMDHN1YmRpcmVjdG9yeZSMA3BrZ5R1YowJZGlyZWN0b3J5lE6MB2FyY2hpdmWUTowFaW5kZXiUTowFc2Rpc3SUTowGd2hlZWxzlE6MFmF0dGVzdGF0aW9uX2lkZW50aXRpZXOUTowEdG9vbJROdWIu', 'expected': {'class': 'packaging.pylock.Package', 'fields': {'name': 'widget-lib', 'version': {'class': 'packaging.version.Version', 'string': '2.0'}, 'marker': None, 'requires_python': None, 'dependencies': None, 'vcs': {'class': 'packaging.pylock.PackageVcs', 'fields': {'type': 'git', 'url': 'https://example.org/repo.git', 'path': None, 'requested_revision': 'stable', 'commit_id': '01234567', 'subdirectory': 'pkg'}}, 'directory': None, 'archive': None, 'index': None, 'sdist': None, 'wheels': None, 'attestation_identities': None, 'tool': None}}}, 'hidden_package_directory': {'class': 'packaging.pylock.PackageDirectory', 'blob': 'gASVZgAAAAAAAACMEHBhY2thZ2luZy5weWxvY2uUjBBQYWNrYWdlRGlyZWN0b3J5lJOUKYGUfZQojARwYXRolIwKdmVuZG9yL3NyY5SMCGVkaXRhYmxllImMDHN1YmRpcmVjdG9yeZSMA3NyY5R1Yi4=', 'expected': {'class': 'packaging.pylock.PackageDirectory', 'fields': {'path': 'vendor/src', 'editable': False, 'subdirectory': 'src'}}}, 'hidden_package_nested': {'class': 'packaging.pylock.Package', 'blob': 'gASVvwEAAAAAAACMEHBhY2thZ2luZy5weWxvY2uUjAdQYWNrYWdllJOUKYGUfZQojARuYW1llIwGd2lkZ2V0lIwHdmVyc2lvbpSMEXBhY2thZ2luZy52ZXJzaW9ulIwHVmVyc2lvbpSTlCmBlChLAEsBSwBLAIeUTk5OTnSUYowGbWFya2VylE6MD3JlcXVpcmVzX3B5dGhvbpROjAxkZXBlbmRlbmNpZXOUTowDdmNzlE6MCWRpcmVjdG9yeZROjAdhcmNoaXZllE6MBWluZGV4lE6MBXNkaXN0lE6MBndoZWVsc5RdlGgAjAxQYWNrYWdlV2hlZWyUk5QpgZR9lChoBYwdd2lkZ2V0LTEuMC4wLXB5My1ub25lLWFueS53aGyUjAt1cGxvYWRfdGltZZROjAN1cmyUTowEcGF0aJROjARzaXpllEuAjAZoYXNoZXOUfZSMBnNoYTI1NpSMQGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGSUc3ViYYwWYXR0ZXN0YXRpb25faWRlbnRpdGllc5ROjAR0b29slE51Yi4=', 'expected': {'class': 'packaging.pylock.Package', 'fields': {'name': 'widget', 'version': {'class': 'packaging.version.Version', 'string': '1.0.0'}, 'marker': None, 'requires_python': None, 'dependencies': None, 'vcs': None, 'directory': None, 'archive': None, 'index': None, 'sdist': None, 'wheels': {'sequence': [{'class': 'packaging.pylock.PackageWheel', 'fields': {'name': 'widget-1.0.0-py3-none-any.whl', 'upload_time': None, 'url': None, 'path': None, 'size': 128, 'hashes': {'map': {'sha256': 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'}}}}]}, 'attestation_identities': None, 'tool': None}}}, 'hidden_package_sdist': {'class': 'packaging.pylock.PackageSdist', 'blob': 'gASVyQAAAAAAAACMEHBhY2thZ2luZy5weWxvY2uUjAxQYWNrYWdlU2Rpc3SUk5QpgZR9lCiMBG5hbWWUjBN3aWRnZXQtMS4wLjAudGFyLmd6lIwLdXBsb2FkX3RpbWWUTowDdXJslE6MBHBhdGiUTowEc2l6ZZRLvIwGaGFzaGVzlH2UjAZzaGEyNTaUjEBjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjlHN1Yi4=', 'expected': {'class': 'packaging.pylock.PackageSdist', 'fields': {'name': 'widget-1.0.0.tar.gz', 'upload_time': None, 'url': None, 'path': None, 'size': 188, 'hashes': {'map': {'sha256': 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'}}}}}, 'hidden_package_wheel': {'class': 'packaging.pylock.PackageWheel', 'blob': 'gASV0wAAAAAAAACMEHBhY2thZ2luZy5weWxvY2uUjAxQYWNrYWdlV2hlZWyUk5QpgZR9lCiMBG5hbWWUjB13aWRnZXQtMS4wLjAtcHkzLW5vbmUtYW55LndobJSMC3VwbG9hZF90aW1llE6MA3VybJROjARwYXRolE6MBHNpemWUS4CMBmhhc2hlc5R9lIwGc2hhMjU2lIxAZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZJRzdWIu', 'expected': {'class': 'packaging.pylock.PackageWheel', 'fields': {'name': 'widget-1.0.0-py3-none-any.whl', 'upload_time': None, 'url': None, 'path': None, 'size': 128, 'hashes': {'map': {'sha256': 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'}}}}}}

for name,row in FIXTURES.items():
    check_payload(name,row)

# Round trips from the NEW format must remain readable, without changing the
# standard list/tuple state emitted by frozen, slotted dataclasses.
for name,row in FIXTURES.items():
    loaded=check_payload(name,row)
    assert isinstance(loaded.__getstate__(),list),(name,type(loaded.__getstate__()))
    for protocol in (2,4,pickle.HIGHEST_PROTOCOL):
        subsequent=pickle.loads(pickle.dumps(loaded,protocol=protocol))
        assert normalize(subsequent)==row['expected'],name

# A malformed old dict must not get silently misread as a list of field names.
# The reference explicitly rejects wrong key sets and wrong list lengths.
from packaging import direct_url,pylock
for cls in (direct_url.VcsInfo,direct_url.DirectUrl,pylock.PackageVcs,pylock.Pylock):
    fields=dataclasses.fields(cls)
    blank=object.__new__(cls)
    names=[f.name for f in fields]
    for state,error_type in (
        ({key:key for key in names[:-1]},TypeError),
        ({**{key:key for key in names},'unexpected':'junk'},TypeError),
        (['value']*(len(fields)-1),ValueError),
        (['value']*(len(fields)+1),ValueError),
    ):
        try:
            blank.__setstate__(state)
        except error_type:
            pass
        else:
            raise AssertionError(f'{cls.__name__} accepted invalid pickle state {type(state).__name__}')

print("R23_PACKAGING_HIDDEN_OK")
'''

_PACKAGING_R23_BAD_STATE_LOOP = """for state,error_type in (
        ({key:key for key in names[:-1]},TypeError),
        ({**{key:key for key in names},'unexpected':'junk'},TypeError),
        (['value']*(len(fields)-1),ValueError),
        (['value']*(len(fields)+1),ValueError),
    ):
        try:
            blank.__setstate__(state)
        except error_type:
            pass
        else:
            raise AssertionError(f'{cls.__name__} accepted invalid pickle state {type(state).__name__}')"""
_PACKAGING_R23B_BAD_STATE_LOOP = """for state in (
        {key:key for key in names[:-1]},
        {**{key:key for key in names},'unexpected':'junk'},
        ['value']*(len(fields)-1),
        ['value']*(len(fields)+1),
    ):
        try:
            blank.__setstate__(state)
        except (TypeError,ValueError):
            pass
        else:
            raise AssertionError(f'{cls.__name__} accepted invalid pickle state {type(state).__name__}')"""


def _packaging_r23b_hidden_source() -> str:
    if PACKAGING_LEGACY_DATACLASS_HIDDEN.count(_PACKAGING_R23_BAD_STATE_LOOP) != 1:
        raise RuntimeError("R23 frozen malformed-state oracle anchor drifted")
    return PACKAGING_LEGACY_DATACLASS_HIDDEN.replace(
        _PACKAGING_R23_BAD_STATE_LOOP, _PACKAGING_R23B_BAD_STATE_LOOP, 1
    )


PACKAGING_PICKLE_VISIBLE = r"""import pickle
import sys
sys.path.insert(0, 'src')
from packaging.metadata import InvalidMetadata
from packaging.dependency_groups import CyclicDependencyGroup

metadata_error = InvalidMetadata('version', 'invalid version field')
metadata_copy = pickle.loads(pickle.dumps(metadata_error))
assert isinstance(metadata_copy, InvalidMetadata)
assert metadata_copy.field == 'version'
assert str(metadata_copy) == 'invalid version field'
assert metadata_copy.args == metadata_error.args

cycle_error = CyclicDependencyGroup('main', 'docs', 'main')
cycle_copy = pickle.loads(pickle.dumps(cycle_error))
assert isinstance(cycle_copy, CyclicDependencyGroup)
assert (
    cycle_copy.requested_group,
    cycle_copy.group,
    cycle_copy.include_group,
) == ('main', 'docs', 'main')
assert str(cycle_copy) == str(cycle_error)
"""


PACKAGING_PICKLE_HIDDEN = r"""import copy
import pickle
import sys
sys.path.insert(0, 'src')
from packaging.metadata import InvalidMetadata
from packaging.dependency_groups import CyclicDependencyGroup

examples = [
    InvalidMetadata('name', 'invalid metadata: café'),
    InvalidMetadata('requires_python', ''),
    CyclicDependencyGroup('root', 'root', 'root'),
    CyclicDependencyGroup('alpha', 'bravo', 'charlie'),
]
for original in examples:
    for protocol in (0, 1, 2, pickle.HIGHEST_PROTOCOL):
        clone = pickle.loads(pickle.dumps(original, protocol=protocol))
        assert type(clone) is type(original)
        assert str(clone) == str(original)
        assert clone.args == original.args
        if isinstance(original, InvalidMetadata):
            assert clone.field == original.field
        else:
            assert (
                clone.requested_group, clone.group, clone.include_group
            ) == (
                original.requested_group, original.group, original.include_group
            )
    for cloner in (copy.copy, copy.deepcopy):
        clone = cloner(original)
        assert type(clone) is type(original)
        assert str(clone) == str(original)
        if isinstance(original, InvalidMetadata):
            assert clone.field == original.field
        else:
            assert clone.include_group == original.include_group

assert str(InvalidMetadata('version', 'bad')) == 'bad'
assert 'self' in str(CyclicDependencyGroup('self', 'self', 'self'))
"""


TOMLKIT_COMMENT_VISIBLE = r"""import tomlkit


def rejects(call):
    try:
        call()
    except ValueError as exc:
        assert "Comment cannot contain" in str(exc), str(exc)
    else:
        raise AssertionError("forbidden comment accepted")


for character in ("\x00", "\x1b", "\x7f"):
    doc = tomlkit.document()
    before = doc.as_string()
    rejects(lambda: doc.add(tomlkit.comment("before" + character + "after")))
    assert doc.as_string() == before

    doc = tomlkit.parse("value = 1 # original\n")
    before = doc.as_string()
    rejects(lambda: doc["value"].comment("before" + character + "after"))
    assert doc.as_string() == before

    doc = tomlkit.parse("values = [1]\n")
    before = doc.as_string()
    rejects(
        lambda: doc["values"].add_line(2, comment="before" + character + "after")
    )
    assert doc.as_string() == before

for text in ("first\nsecond", "first\rsecond"):
    doc = tomlkit.parse("value = 1\n")
    before = doc.as_string()
    rejects(lambda: doc["value"].comment(text))
    assert doc.as_string() == before

    doc = tomlkit.parse("values = [1]\n")
    before = doc.as_string()
    rejects(lambda: doc["values"].add_line(2, comment=text))
    assert doc.as_string() == before

rejects(lambda: tomlkit.comment("first\rafter"))

for text in ("first\nsecond\n", "first\r\nsecond\r\n"):
    doc = tomlkit.document()
    doc.add(tomlkit.comment(text))
    rendered = doc.as_string()
    assert "first" in rendered and "second" in rendered
    assert tomlkit.parse(rendered).as_string() == rendered

valid = "tab\tunicode \u00e9 \u0085 \u2028"
doc = tomlkit.document()
doc.add(tomlkit.comment(valid))
assert valid in doc.as_string()
assert tomlkit.parse(doc.as_string()).as_string() == doc.as_string()

doc = tomlkit.parse("value = 1\n")
doc["value"].comment(valid)
assert valid in doc.as_string()
assert tomlkit.parse(doc.as_string()).as_string() == doc.as_string()

doc = tomlkit.parse("values = [1]\n")
doc["values"].add_line(2, comment=valid)
assert valid in doc.as_string()
assert tomlkit.parse(doc.as_string()).as_string() == doc.as_string()
"""


TOMLKIT_COMMENT_HIDDEN = r"""import tomllib

import tomlkit

FORBIDDEN = [chr(code) for code in range(32) if code not in (9, 10, 13)] + [chr(127)]


def rejected(call):
    try:
        call()
    except ValueError as exc:
        assert "Comment cannot contain" in str(exc), repr(exc)
    else:
        raise AssertionError("comment containing a forbidden control was accepted")


for character in FORBIDDEN:
    payload = "left" + character + "right"

    doc = tomlkit.document()
    before = doc.as_string()
    rejected(lambda: doc.add(tomlkit.comment(payload)))
    assert doc.as_string() == before

    doc = tomlkit.parse("value = 1 # keep\n")
    before = doc.as_string()
    rejected(lambda: doc["value"].comment(payload))
    assert doc.as_string() == before

    doc = tomlkit.parse("values = [1]\n")
    before = doc.as_string()
    rejected(lambda: doc["values"].add_line(2, comment=payload))
    assert doc.as_string() == before

for payload in ("a\rb", "a\r\rb", "a\r\nb\rc", "a\r\r\nb"):
    rejected(lambda payload=payload: tomlkit.comment(payload))

for payload in ("a\nb", "a\r\nb"):
    doc = tomlkit.document()
    doc.add(tomlkit.comment(payload))
    rendered = doc.as_string()
    assert tomlkit.parse(rendered).as_string() == rendered
    assert tomllib.loads(rendered) == {}

valid = "tab\tUnicode é \u0085 \u2028 supplementary \U0001f642"
for entry in ("standalone", "item", "array"):
    doc = tomlkit.document()
    if entry == "standalone":
        doc.add(tomlkit.comment(valid))
    elif entry == "item":
        doc.add("value", 1)
        doc["value"].comment(valid)
    else:
        doc.add("values", [1])
        doc["values"].add_line(2, comment=valid)
    rendered = doc.as_string()
    assert valid in rendered
    assert tomlkit.parse(rendered).as_string() == rendered
    tomllib.loads(rendered)
"""


PLUGGY_MODULE_SPLIT_VISIBLE = r"""import sys

sys.path.insert(0, "src")

import pluggy
import pluggy._callers as legacy_callers
import pluggy._hooks as legacy_hooks
from pluggy._caller import (
    HookCaller,
    HookRelay,
    _HookCaller,
    _HookExec,
    _HookRelay,
    _SubsetHookCaller,
)
from pluggy._config import HookimplOpts, HookspecOpts, normalize_hookimpl_opts
from pluggy._decorators import (
    HookimplMarker,
    HookSpec,
    HookspecMarker,
    _Namespace,
    varnames,
)
from pluggy._execution import (
    Teardown,
    _multicall,
    _raise_wrapfail,
    _warn_teardown_exception,
    run_old_style_hookwrapper,
)
from pluggy._impl import HookImpl, _HookImplFunction, _Plugin

assert legacy_hooks.HookCaller is HookCaller
assert legacy_hooks.HookRelay is HookRelay
assert legacy_hooks.HookImpl is HookImpl
assert legacy_hooks.HookSpec is HookSpec
assert legacy_hooks.HookimplMarker is HookimplMarker
assert legacy_hooks.HookspecMarker is HookspecMarker
assert legacy_hooks.HookimplOpts is HookimplOpts
assert legacy_hooks.HookspecOpts is HookspecOpts
assert legacy_hooks.normalize_hookimpl_opts is normalize_hookimpl_opts
assert legacy_hooks.varnames is varnames
for name, value in {
    "_HookCaller": _HookCaller,
    "_HookExec": _HookExec,
    "_HookRelay": _HookRelay,
    "_SubsetHookCaller": _SubsetHookCaller,
    "_Namespace": _Namespace,
    "_Plugin": _Plugin,
    "_HookImplFunction": _HookImplFunction,
}.items():
    assert getattr(legacy_hooks, name) is value

assert legacy_callers._multicall is _multicall
assert legacy_callers._raise_wrapfail is _raise_wrapfail
assert legacy_callers._warn_teardown_exception is _warn_teardown_exception
assert legacy_callers.run_old_style_hookwrapper is run_old_style_hookwrapper
assert legacy_callers.Teardown is Teardown
assert set(legacy_hooks.__all__) == {
    "HookCaller", "HookImpl", "HookRelay", "HookSpec", "HookimplMarker",
    "HookimplOpts", "HookspecMarker", "HookspecOpts", "_HookCaller",
    "_HookExec", "_HookImplFunction", "_HookRelay", "_Namespace", "_Plugin",
    "_SubsetHookCaller", "normalize_hookimpl_opts", "varnames",
}
assert set(legacy_callers.__all__) == {
    "Teardown", "_multicall", "_raise_wrapfail", "_warn_teardown_exception",
    "run_old_style_hookwrapper",
}

hookspec = HookspecMarker("r10-visible")
hookimpl = HookimplMarker("r10-visible")


class Spec:
    @hookspec
    def transform(self, value): ...


class Plugin:
    @hookimpl
    def transform(self, value):
        return value * 2


pm = pluggy.PluginManager("r10-visible")
pm.add_hookspecs(Spec)
plugin = Plugin()
pm.register(plugin)
assert pm.hook.transform(value=6) == [12]
assert isinstance(pm.hook, HookRelay)
assert isinstance(pm.hook.transform, HookCaller)
assert isinstance(pm.hook.transform._hookimpls[0], HookImpl)
assert pluggy.HookCaller is HookCaller
assert pluggy.HookImpl is HookImpl
assert pluggy.HookRelay is HookRelay
assert pluggy.HookspecMarker is HookspecMarker
assert pluggy.HookimplMarker is HookimplMarker
"""


PLUGGY_MODULE_SPLIT_HIDDEN = r"""import sys

sys.path.insert(0, "src")

import pluggy
from pluggy._caller import HookCaller, HookRelay, _SubsetHookCaller
from pluggy._config import HookimplOpts, HookspecOpts, normalize_hookimpl_opts
from pluggy._decorators import HookimplMarker, HookSpec, HookspecMarker, varnames
from pluggy._execution import _multicall, _raise_wrapfail, _warn_teardown_exception
from pluggy._hooks import HookImpl as LegacyHookImpl
from pluggy._hooks import HookCaller as LegacyHookCaller
from pluggy._hooks import HookRelay as LegacyHookRelay
from pluggy._callers import _multicall as legacy_multicall

assert legacy_multicall is _multicall
assert LegacyHookCaller is HookCaller
assert LegacyHookRelay is HookRelay
assert LegacyHookImpl.__module__ == "pluggy._impl"
assert HookCaller.__module__ == "pluggy._caller"
assert HookRelay.__module__ == "pluggy._caller"
assert HookSpec.__module__ == "pluggy._decorators"
assert HookimplMarker.__module__ == "pluggy._decorators"
assert HookspecMarker.__module__ == "pluggy._decorators"
assert varnames.__module__ == "pluggy._decorators"
assert HookspecOpts.__module__ == "pluggy._config"
assert HookimplOpts.__module__ == "pluggy._config"
assert normalize_hookimpl_opts.__module__ == "pluggy._config"
assert _multicall.__module__ == "pluggy._execution"
assert _raise_wrapfail.__module__ == "pluggy._execution"
assert _warn_teardown_exception.__module__ == "pluggy._execution"


def wrapper_case(old_style):
    spec = HookspecMarker("r10-wrapper")
    impl = HookimplMarker("r10-wrapper")
    events = []

    class Specs:
        @spec
        def calculate(self, value): ...

    class Worker:
        @impl
        def calculate(self, value):
            events.append("worker")
            return value + 1

    if old_style:
        class Wrapper:
            @impl(hookwrapper=True)
            def calculate(self, value):
                events.append("before")
                outcome = yield
                assert outcome.get_result() == [value + 1]
                events.append("after")
    else:
        class Wrapper:
            @impl(wrapper=True)
            def calculate(self, value):
                events.append("before")
                result = yield
                events.append("after")
                return result

    pm = pluggy.PluginManager("r10-wrapper")
    pm.add_hookspecs(Specs)
    pm.register(Worker())
    pm.register(Wrapper())
    assert pm.hook.calculate(value=4) == [5]
    assert events == ["before", "worker", "after"]
    assert isinstance(pm.hook.calculate, HookCaller)


wrapper_case(False)
wrapper_case(True)

spec = HookspecMarker("r10-first")
impl = HookimplMarker("r10-first")


class FirstSpec:
    @spec(firstresult=True)
    def choose(self): ...


class Empty:
    @impl(tryfirst=True)
    def choose(self):
        return None


class Selected:
    @impl
    def choose(self):
        return "selected"


pm = pluggy.PluginManager("r10-first")
pm.add_hookspecs(FirstSpec)
pm.register(Empty())
pm.register(Selected())
assert pm.hook.choose() == "selected"

spec = HookspecMarker("r10-historic")
impl = HookimplMarker("r10-historic")


class HistoricSpec:
    @spec(historic=True)
    def announce(self, value): ...


seen = []


class LatePlugin:
    @impl
    def announce(self, value):
        seen.append(value)


pm = pluggy.PluginManager("r10-historic")
pm.add_hookspecs(HistoricSpec)
pm.hook.announce.call_historic(kwargs={"value": 17})
pm.register(LatePlugin())
assert seen == [17]

spec = HookspecMarker("r10-subset")
impl = HookimplMarker("r10-subset")


class SubsetSpec:
    @spec
    def collect(self): ...


class A:
    @impl
    def collect(self):
        return "a"


class B:
    @impl
    def collect(self):
        return "b"


a, b = A(), B()
pm = pluggy.PluginManager("r10-subset")
pm.add_hookspecs(SubsetSpec)
pm.register(a)
pm.register(b)
subset = pm.subset_hook_caller("collect", remove_plugins=[a])
assert isinstance(subset, HookCaller)
assert isinstance(subset, _SubsetHookCaller)
assert subset() == ["b"]
"""


BOLTONS_BULK_VISIBLE = r"""import sys

sys.path.insert(0, ".")
from boltons.listutils import BarrelList
from boltons.setutils import IndexedSet


def spread_list():
    result = BarrelList(range(30000))
    assert result.pop(0) == 0
    result.insert(0, 0)
    assert len(result.lists) > 1
    return result


items = spread_list()
reference = list(range(30000))
try:
    items[len(items)]
except IndexError:
    pass
else:
    raise AssertionError("index beyond end was accepted")

items.insert(len(items), "last")
reference.insert(len(reference), "last")
items.insert(1_000_000, "later")
reference.insert(1_000_000, "later")
items.insert(-1_000_000, "first")
reference.insert(-1_000_000, "first")
assert list(items) == reference

ordered = IndexedSet(range(5000))
before_compactions = ordered._compactions
ordered.difference_update(set(range(0, 5000, 2)))
assert list(ordered) == list(range(1, 5000, 2))
assert ordered._compactions - before_compactions <= 1
assert ordered.index(2501) == 1250
ordered.add(0)
assert ordered[-1] == 0

other = IndexedSet(range(5000))
before_compactions = other._compactions
other.intersection_update(set(range(2500, 9000)))
assert list(other) == list(range(2500, 5000))
assert other._compactions - before_compactions <= 1
"""


BOLTONS_BULK_HIDDEN = r"""import sys

sys.path.insert(0, ".")
from boltons.listutils import BarrelList
from boltons.setutils import IndexedSet


def split_barrel(size):
    result = BarrelList(range(size))
    assert result.pop(0) == 0
    result.insert(0, 0)
    assert len(result.lists) > 1
    return result


# Boundaries must behave like list for multi-sublist storage, including drains.
barrel = split_barrel(28000)
expected = list(range(28000))
for offset in (0, 1, 5, 500):
    try:
        barrel[len(barrel) + offset]
    except IndexError:
        pass
    else:
        raise AssertionError("out-of-bounds indexed access did not fail")
for index, item in ((len(expected), -101), (len(expected) + 999, -102), (-len(expected) - 9, -103)):
    barrel.insert(index, item)
    expected.insert(index, item)
assert list(barrel) == expected

tail = split_barrel(30000)
tail_reference = list(range(30000))
tail_length = len(tail.lists[-1])
for _ in range(tail_length):
    assert tail.pop(len(tail) - 1) == tail_reference.pop()
assert tail_reference
assert tail.pop() == tail_reference.pop()
assert list(tail) == tail_reference

# Bulk updates must preserve iteration order, uniqueness, indexes, and a
# bounded number of full compactions, even across later mutations.
for kind in ("difference", "intersection"):
    values = IndexedSet(range(8000))
    initial_compactions = values._compactions
    if kind == "difference":
        excluded = set(range(0, 8000, 2))
        values.difference_update(excluded)
        expected_values = list(range(1, 8000, 2))
    else:
        allowed = set(range(2000, 6500))
        values.intersection_update(allowed)
        expected_values = list(range(2000, 6500))
    assert list(values) == expected_values
    assert values._compactions - initial_compactions <= 1
    assert len(values) == len(expected_values)
    assert values[0] == expected_values[0] and values[-1] == expected_values[-1]
    assert values.index(expected_values[len(expected_values) // 2]) == len(expected_values) // 2
    values.add(-7)
    assert values[-1] == -7
    values.discard(-7)
    assert list(values) == expected_values

small = IndexedSet(range(25))
small.difference_update({1, 5, 11})
assert list(small) == [i for i in range(25) if i not in (1, 5, 11)]
small.intersection_update({0, 3, 12, 22, 99})
assert list(small) == [0, 3, 12, 22]
"""


RICH_SOFT_WRAP_VISIBLE = r"""import os
os.environ["TERM"] = "xterm-256color"
os.environ.pop("NO_COLOR", None)

from rich.console import Console

def rendered_lines(lines):
    console = Console(
        color_system="standard", force_terminal=True, width=60,
        _environ={"TERM": "xterm-256color"},
    )
    with console.capture() as capture:
        for content, options in lines:
            console.print(content, **options)
    return capture.get()

# Color must be reset before the *physical* newline so that the next line
# never inherits the previous line's background in a terminal that scrolls.
actual = rendered_lines([
    ("ready", {"style": "blue on white", "soft_wrap": True}),
    ("next", {}),
])
assert actual == "\x1b[34;47mready\x1b[0m\nnext\n", repr(actual)

# Embedded newlines must remain and each nonempty line must be styled
# independently, not as one style spanning a linefeed.
actual = rendered_lines([
    ("north\nsouth", {"style": "red on blue", "soft_wrap": True}),
    ("end", {}),
])
assert actual == (
    "\x1b[31;44mnorth\x1b[0m\n"
    "\x1b[31;44msouth\x1b[0m\nend\n"
), repr(actual)

# Preserve the control paths: no style and ordinary (non-soft) wrapping.
assert rendered_lines([("text", {"soft_wrap": True})]) == "text\n"
assert rendered_lines([("plain", {"style": "red on blue", "soft_wrap": False})]) == (
    "\x1b[31;44mplain\x1b[0m\n"
)
print("RICH_SOFT_WRAP_VISIBLE_OK")
"""

RICH_SOFT_WRAP_HIDDEN = r"""import os
os.environ["TERM"] = "xterm-256color"
os.environ.pop("NO_COLOR", None)

from rich.console import Console
from rich.text import Text

def render(*items):
    console = Console(
        color_system="standard", force_terminal=True, width=80,
        _environ={"TERM": "xterm-256color"},
    )
    with console.capture() as buffer:
        for item, style, soft in items:
            console.print(item, style=style, soft_wrap=soft)
    return buffer.get()

# Multiple empty lines must not inherit a colored background; following
# unstyled output must be uncolored, including after a blank logical line.
assert render(("a\n\nb", "blue on white", True), ("tail", None, False)) == (
    "\x1b[34;47ma\x1b[0m\n\n"
    "\x1b[34;47mb\x1b[0m\ntail\n"
)

# Rendering a pre-styled Text with an additional print style must preserve
# the combined style on characters but reset before each line separator.
text = Text("first\nsecond", style="bold green")
assert render((text, "red on blue", True), ("tail", None, False)) == (
    "\x1b[1;32;44mfirst\x1b[0m\n"
    "\x1b[1;32;44msecond\x1b[0m\ntail\n"
)

# Maintain real text, blank lines, and the existing non-soft-wrap result.
uncolored = render(("line1\n\nline3", None, True))
assert uncolored == "line1\n\nline3\n", repr(uncolored)
assert render(("unchanged", "red on blue", False)) == (
    "\x1b[31;44munchanged\x1b[0m\n"
)
print("RICH_SOFT_WRAP_HIDDEN_OK")
"""


CLICK_CONTEXT_SETTINGS_VISIBLE = r"""import sys
sys.path.insert(0, 'src')

import click
from click.testing import CliRunner


@click.command(context_settings={'color': False, 'terminal_width': 91})
@click.pass_context
def report(ctx):
    click.echo(f'width={ctx.terminal_width};color={ctx.color};obj={ctx.obj}')


runner = CliRunner()
# Existing **extra behavior must remain valid when the new parameter is absent.
previous = runner.invoke(report, obj='previous')
assert previous.exit_code == 0, (previous.output, previous.exception)
assert previous.output.strip() == 'width=91;color=False;obj=previous', previous.output

# New settings bypass CliRunner.invoke(color=...) and take precedence over
# the command's declared context defaults, not just missing settings.
settings = {'color': True, 'terminal_width': 41, 'obj': 'caller'}
new = runner.invoke(report, args=[], color=False, context_settings=settings)
assert new.exit_code == 0, (new.output, new.exception)
assert new.output.strip() == 'width=41;color=True;obj=caller', new.output
assert settings == {'color': True, 'terminal_width': 41, 'obj': 'caller'}

# An empty override behaves exactly like the pre-feature call.
empty = runner.invoke(report, context_settings={})
assert empty.exit_code == 0, (empty.output, empty.exception)
assert empty.output.strip() == 'width=91;color=False;obj=None'
"""


CLICK_CONTEXT_SETTINGS_HIDDEN = r"""import sys
sys.path.insert(0, 'src')

import click
from click.testing import CliRunner


@click.group(context_settings={'terminal_width': 88, 'color': False})
@click.pass_context
def cli(ctx):
    pass


@cli.command()
@click.option('--source', default='normal')
@click.pass_context
def inspect(ctx, source):
    root = ctx.find_root()
    click.echo(f'{source}|{root.terminal_width}|{ctx.terminal_width}|{root.color}|{root.obj}')


runner = CliRunner()
options = {'default_map': {'inspect': {'source': 'from-map'}},
           'terminal_width': 37, 'color': True, 'obj': 'audit'}
result = runner.invoke(cli, ['inspect'], context_settings=options, color=False)
assert result.exit_code == 0, (result.output, result.exception)
assert result.output.strip() == 'from-map|37|37|True|audit', result.output
assert options['terminal_width'] == 37

# Backwards-compatible direct keyword Context settings through **extra.
legacy = runner.invoke(cli, ['inspect', '--source', 'explicit'],
                       terminal_width=70, obj='legacy')
assert legacy.exit_code == 0, (legacy.output, legacy.exception)
assert legacy.output.strip() == 'explicit|70|70|False|legacy', legacy.output

# An explicit new context setting must not be intercepted by similarly named
# CliRunner parameters (including the capture-time color switch).
@click.command()
@click.pass_context
def colors(ctx):
    click.echo(f'context-color={ctx.color}')

result = runner.invoke(colors, color=False, context_settings={'color': True})
assert result.exit_code == 0, (result.output, result.exception)
assert result.output.strip() == 'context-color=True', result.output
"""


TOOLZ_PARTITION_LENGTH_VISIBLE = r"""from toolz.itertoolz import partition_all

class Misreported(list):
    def __init__(self, values, delta):
        super().__init__(values)
        self.delta = delta

    def __len__(self):
        return super().__len__() + self.delta


def must_fail(values, size, delta):
    sequence = Misreported(values, delta)
    try:
        list(partition_all(size, sequence))
    except LookupError:
        return
    raise AssertionError(
        f"invalid reported length silently accepted for {len(values)=}, {size=}, {delta=}"
    )

# A length estimate must not silently change the final partition's contents.
must_fail([10, 20], 5, +1)
must_fail([10, 20], 5, -1)
must_fail([1, 2, 3, 4, 5], 3, +1)
must_fail([1, 2, 3, 4, 5], 3, -1)

# Correct-sized lists, exact multiples, empty inputs and one-shot iterators
# preserve their original tuple semantics.
assert list(partition_all(4, list(range(10)))) == [
    (0, 1, 2, 3), (4, 5, 6, 7), (8, 9)
]
assert list(partition_all(3, range(9))) == [
    (0, 1, 2), (3, 4, 5), (6, 7, 8)
]
assert list(partition_all(4, iter([1, 2, 3]))) == [(1, 2, 3)]
assert list(partition_all(7, [])) == []
"""


TOOLZ_PARTITION_LENGTH_HIDDEN = r"""from toolz.itertoolz import partition_all

class LengthLiar(list):
    def __init__(self, data, *, difference):
        super().__init__(data)
        self.difference = difference

    def __len__(self):
        return super().__len__() + self.difference


def raises_lookup(value, chunk_size):
    try:
        tuple(partition_all(chunk_size, value))
    except LookupError:
        return True
    return False

# Use different sizes and input payloads from visible checks: misreported
# partially-filled chunks must fail, even if earlier full chunks were emitted.
for size, data in (
    (4, ['a', 'b', 'c']),
    (5, [None, False, 'x', 3]),
    (6, list(range(8))),
    (7, list(range(10))),
):
    for difference in (-1, 1):
        assert raises_lookup(LengthLiar(data, difference=difference), size), (
            size, difference
        )

# Iterables that cannot report a length still produce every element in order,
# and unchanged valid source lengths preserve normal chunk boundaries.
class NoLength:
    def __iter__(self):
        yield from ('a', None, False, 9, 10)

    def __len__(self):
        raise TypeError('no size known')

assert list(partition_all(4, NoLength())) == [('a', None, False, 9), (10,)]
assert list(partition_all(1, [9, 8])) == [(9,), (8,)]
assert list(partition_all(3, [1, 2, 3, 4, 5, 6, 7])) == [
    (1, 2, 3), (4, 5, 6), (7,)
]
"""


MORE_ITERTOOLS_PEEKABLE_VISIBLE = r"""from itertools import count, islice

from more_itertools import peekable

# A reverse slice with a concrete nonnegative start and omitted stop must
# never exhaust an unbounded source. Fail promptly if too much is pulled.
seen = []
def guarded():
    for value in count():
        if len(seen) >= 7:
            raise AssertionError("reverse slice consumed beyond its rightmost index")
        seen.append(value)
        yield value

items = peekable(guarded())
assert items[5::-1] == [5, 4, 3, 2, 1, 0]
assert seen == list(range(6)), seen
assert items[5::-2] == [5, 3, 1]
assert seen == list(range(6)), seen
assert list(islice(items, 6)) == list(range(6))
assert seen == list(range(6)), seen

# Ordinary forward slices must continue working.
forward = peekable(range(12))
assert forward[2:7:2] == [2, 4, 6]
assert next(forward) == 0
print("MORE_ITERTOOLS_PEEKABLE_VISIBLE_OK")
"""

MORE_ITERTOOLS_PEEKABLE_HIDDEN = r"""from itertools import count, islice

from more_itertools import peekable

seen = []
def guarded():
    for value in count(10):
        if len(seen) >= 12:
            raise AssertionError("read past requested slice window")
        seen.append(value)
        yield value

# A partially prefetched cache must be reused; a second reverse slice
# with an omitted stop must not pull the source again.
items = peekable(guarded())
assert items[2] == 12
assert len(seen) == 3
assert items[10::-3] == [20, 17, 14, 11]
assert seen == list(range(10, 21)), seen
assert items[10::-1] == list(range(20, 9, -1))
assert seen == list(range(10, 21)), seen
assert list(islice(items, 11)) == list(range(10, 21))
assert seen == list(range(10, 21)), seen

# Large finite streams must also remain lazy rather than needlessly cached.
read_count = [0]
def large():
    for value in range(2000):
        read_count[0] += 1
        yield value

items = peekable(large())
assert items[4::-1] == [4, 3, 2, 1, 0]
assert read_count == [5], read_count
assert next(items) == 0

# True right-relative indexes and omitted starts still require the finite
# source's full extent. Preserve Python's reverse slicing semantics.
assert peekable(range(8))[::-2] == [7, 5, 3, 1]
assert peekable(range(8))[-1] == 7
assert peekable(range(8))[7:-9:-2] == [7, 5, 3, 1]

print("MORE_ITERTOOLS_PEEKABLE_HIDDEN_OK")
"""


PYFLAKES_TYPING_FIELDS_VISIBLE = """from io import StringIO

from pyflakes.api import check
from pyflakes.reporter import Reporter

source = '''from typing import NamedTuple, TypedDict
from schema_catalog import columns, properties

Profile = TypedDict("Profile", properties)
Visit = NamedTuple("Visit", columns)
'''

warnings, errors = StringIO(), StringIO()
count = check(source, "profiles.py", Reporter(warnings, errors))
assert count == 0, warnings.getvalue() + errors.getvalue()

print("PYFLAKES_TYPING_FIELDS_VISIBLE_OK")
"""


PYFLAKES_TYPING_FIELDS_HIDDEN = """from io import StringIO

from pyflakes.api import check
from pyflakes.messages import UndefinedName, UnusedImport
from pyflakes.reporter import Reporter


class RecordingReporter(Reporter):
    def __init__(self):
        self.errors = StringIO()
        super().__init__(StringIO(), self.errors)
        self.diagnostics = []

    def flake(self, message):
        self.diagnostics.append((type(message), message.message_args))


def expect_diagnostics(filename, source, *expected):
    reporter = RecordingReporter()
    count = check(source, filename, reporter)
    assert not reporter.errors.getvalue(), reporter.errors.getvalue()
    actual = tuple(reporter.diagnostics)
    assert count == len(expected) and actual == expected, (filename, actual)


expect_diagnostics(
    "runtime_fields.py",
    '''import typing as types
import field_catalog as catalog
from field_catalog import select_fields, stored_rows

Packet = types.TypedDict("Packet", catalog.mappings["wire"])
Record = types.NamedTuple("Record", select_fields(stored_rows, "stored"))
''',
)

expect_diagnostics(
    "missing_fields.py",
    '''from typing import NamedTuple as record, TypedDict as mapping
from field_catalog import select_fields, surplus

Event = mapping("Event", unknown_schema)
Entry = record("Entry", select_fields(unknown_fields))
''',
    (UnusedImport, ("field_catalog.surplus",)),
    (UndefinedName, ("unknown_schema",)),
    (UndefinedName, ("unknown_fields",)),
)

expect_diagnostics(
    "forward_references.py",
    '''from typing import NamedTuple, TypedDict

Payload = TypedDict("Payload", {"value": "Value", "note": "UnknownNote"})
Row = NamedTuple("Row", [("value", "Value"), ("tag", "UnknownTag")])

class Value:
    pass
''',
    (UndefinedName, ("UnknownNote",)),
    (UndefinedName, ("UnknownTag",)),
)

print("PYFLAKES_TYPING_FIELDS_HIDDEN_OK")
"""


@dataclass(frozen=True)
class RepositoryTask:
    source: str
    upstream: str
    base_sha: str
    reference_fix_sha: str
    editable: frozenset[str]
    visible_script: str
    visible_source: str
    hidden_source: str
    command_grant: str
    prompt: str
    hidden_expected_json: str | None = None
    runtime: "PythonSourceRuntime | None" = None
    upstream_pr: str | None = None
    license: str | None = None
    require_all_editable_changed: bool = True
    expected_reference_paths: frozenset[str] | None = None
    require_both_oracles_red_green: bool = False
    require_single_visible_test: bool = False
    require_each_editable_ablation_red: bool = False
    diagnostic_retake_of: str | None = None


@dataclass(frozen=True)
class RuntimePackage:
    distribution: str
    package: str
    version: str
    trusted_source_sha256: str


@dataclass(frozen=True)
class PythonSourceRuntime:
    """A tiny source-only overlay needed by a pinned Python checkout."""

    packages: tuple[RuntimePackage, ...]
    generated_files: tuple[tuple[str, bytes], ...]
    trusted_overlay_sha256: str


PYTEST_R17_RUNTIME = PythonSourceRuntime(
    packages=(
        RuntimePackage(
            "pluggy", "pluggy", "1.6.0",
            "730093250a37d7d717dd7e7f1437bbedac53b1b378c79ecd27ae255b19ef2304",
        ),
        RuntimePackage(
            "iniconfig", "iniconfig", "2.3.0",
            "fbb6f8782db1b0c49aafda68456e08ffe2051c2b6b69b61fde9f04313cf949de",
        ),
        RuntimePackage(
            "Pygments", "pygments", "2.21.0",
            "eef8783697c9205496d5c357e9be17a75e0d3895d02e7cd9e4b4b5a6f250210b",
        ),
        RuntimePackage(
            "packaging", "packaging", "26.3",
            "eb4f1d0f37a10fe5ac03972ecba52dd94f2cd0f06bf66dcc61dd0c10af736764",
        ),
    ),
    generated_files=(
        (
            "src/_pytest/_version.py",
            b'"""Disposable offline benchmark equivalent of setuptools_scm generated metadata."""\n'
            b'__version__ = version = "9.1.0.dev0"\n'
            b'__version_tuple__ = version_tuple = (9, 1, 0, "dev0")\n'
            b"__commit_id__ = commit_id = None\n",
        ),
    ),
    trusted_overlay_sha256=(
        "06f72d92dfa1f1bc0b9b1863ec21b5deee01cdc5f721f6ae148ae225a4bc0db7"
    ),
)


PYTEST_MRO_MARKER_RUNTIME = replace(
    PYTEST_R17_RUNTIME,
    generated_files=(
        (
            "src/_pytest/_version.py",
            b'"""Disposable offline benchmark equivalent of setuptools_scm generated metadata."""\n'
            b'__version__ = version = "9.2.0.dev0"\n'
            b'__version_tuple__ = version_tuple = (9, 2, 0, "dev0")\n'
            b"__commit_id__ = commit_id = None\n",
        ),
    ),
)


PYTEST_MIXED_INDIRECT_VISIBLE = r'''from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


PYTESTS = r"""
import pytest


@pytest.fixture(params=("fixture-left", "fixture-right"))
def shade(request):
    return "fixture:" + request.param


@pytest.fixture
def wrapped(request, shade):
    return request.param, shade


@pytest.mark.parametrize(
    ("wrapped", "shade"),
    (("first", "direct-a"), ("second", "direct-b")),
    indirect=["wrapped"],
)
def test_list_indirect_keeps_direct_fixture_name(wrapped, shade):
    assert wrapped[0] in {"first", "second"}
    assert wrapped[1] == shade
    assert shade in {"direct-a", "direct-b"}


@pytest.mark.parametrize(
    ("shade", "wrapped"),
    (("direct-c", "third"), ("direct-d", "fourth")),
    indirect=["wrapped"],
)
def test_reversed_argument_order_keeps_direct_value(shade, wrapped):
    assert wrapped[0] in {"third", "fourth"}
    assert wrapped[1] == shade
    assert shade in {"direct-c", "direct-d"}


@pytest.fixture
def token(request):
    return "token:" + request.param


@pytest.mark.parametrize("token", ("one", "two"), indirect=True)
def test_boolean_true_routes_values_through_fixture(token):
    assert token in {"token:one", "token:two"}


@pytest.mark.parametrize("shade", ("plain-a", "plain-b"), indirect=False)
def test_boolean_false_keeps_values_direct(shade):
    assert shade in {"plain-a", "plain-b"}


@pytest.mark.parametrize("number", (3, 8))
def test_ordinary_parametrize_path(number):
    assert number in {3, 8}
"""

MISSING_FIXTURE = r"""
import pytest


@pytest.mark.parametrize("value", (1,), indirect=["missing"])
def test_unknown_indirect_fixture(value):
    assert value == 1
"""

CHILD = r"""
import importlib
from pathlib import Path
import sys

source, overlay, test_file = map(Path, sys.argv[1:4])
source = source.resolve()
overlay = overlay.resolve()
sys.path[:0] = [str(source), str(overlay)]

import pytest

for name in ("pluggy", "iniconfig", "pygments", "packaging"):
    module = importlib.import_module(name)
    origin = Path(module.__file__).resolve()
    if not origin.is_relative_to(overlay):
        raise AssertionError(f"{name} did not load from the pinned runtime overlay")
if not Path(pytest.__file__).resolve().is_relative_to(source):
    raise AssertionError("pytest did not load from this pinned source checkout")
if not (source / "_pytest" / "_version.py").is_file():
    raise AssertionError("generated version metadata is missing")

result = pytest.main([
    "-q", "-p", "no:cacheprovider", "--noconftest", str(test_file)
])
sys.exit(int(result))
"""


def run_cases(root: Path, name: str, source_text: str) -> tuple[int, str]:
    test_file = root / f"{name}.py"
    test_file.write_text(source_text, encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONNOUSERSITE": "1",
    }
    completed = subprocess.run(
        [
            sys.executable, "-B", "-S", "-c", CHILD,
            str(Path(__file__).resolve().parent / "src"),
            str(Path(__file__).resolve().parent / ".benchmark_runtime" / "site"),
            str(test_file),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


if Path(sys.executable).resolve() != Path("/usr/bin/python3").resolve():
    raise SystemExit("R17 requires the same /usr/bin/python3 used by the grader")
if sys.version_info < (3, 10):
    raise SystemExit("R17 requires Python 3.10 or newer")

with tempfile.TemporaryDirectory(prefix="pytest-r17-visible-") as temporary:
    root = Path(temporary)
    valid_exit, valid_output = run_cases(root, "valid_cases", PYTESTS)
    if valid_exit != 0 or "10 passed" not in valid_output:
        raise AssertionError(f"valid pytest cases failed: {valid_output[-1200:]}")
    invalid_exit, invalid_output = run_cases(
        root, "invalid_case", MISSING_FIXTURE
    )
    if (
        invalid_exit != 2
        or "indirect fixture 'missing' doesn't exist" not in invalid_output.lower()
    ):
        raise AssertionError(f"missing indirect fixture was not diagnosed: {invalid_output[-1200:]}")

print("PYTEST_MIXED_INDIRECT_VISIBLE_OK")
'''


PYTEST_MIXED_INDIRECT_HIDDEN = r'''from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


HIDDEN_CASES = r"""
import json
import pytest


@pytest.fixture(params=("unused-0", "unused-1"))
def flavor(request):
    return request.param


@pytest.fixture
def left(request, flavor):
    return (request.param, flavor)


@pytest.fixture
def right(request, flavor):
    return (request.param, flavor)


@pytest.mark.parametrize(
    ("right", "flavor", "left"),
    (("R0", "chosen-0", "L0"), ("R1", "chosen-1", "L1")),
    indirect=["right", "left"],
)
def test_multiple_indirect_arguments_share_direct_capture(right, flavor, left):
    print("R17_OBSERVATION:" + json.dumps(["multiple", right, flavor, left]))


@pytest.fixture
def first(request):
    return "first=" + request.param


@pytest.fixture
def second(request):
    return "second=" + request.param


@pytest.mark.parametrize(
    ("first", "second"), (("a", "x"), ("b", "y")), indirect=True
)
def test_boolean_true_applies_to_every_argument(first, second):
    print("R17_OBSERVATION:" + json.dumps(["true", first, second]))


@pytest.mark.parametrize("flavor", ("direct-1", "direct-2"), indirect=False)
def test_direct_false_shadows_parametrized_fixture(flavor):
    print("R17_OBSERVATION:" + json.dumps(["false", flavor]))


@pytest.mark.parametrize("count", (0, 5, 11))
def test_unmarked_parametrize_stays_ordinary(count):
    print("R17_OBSERVATION:" + json.dumps(["ordinary", count]))
"""

UNKNOWN_CASE = r"""
import pytest


@pytest.mark.parametrize("value", ("x",), indirect=["absent"])
def test_unknown_indirect_name(value):
    print(value)
"""

CHILD = r"""
import importlib
from pathlib import Path
import sys

source, overlay, test_file = map(Path, sys.argv[1:4])
source = source.resolve()
overlay = overlay.resolve()
sys.path[:0] = [str(source), str(overlay)]
import pytest
for name in ("pluggy", "iniconfig", "pygments", "packaging"):
    origin = Path(importlib.import_module(name).__file__).resolve()
    if not origin.is_relative_to(overlay):
        raise RuntimeError(f"unexpected runtime package origin: {name}")
if not Path(pytest.__file__).resolve().is_relative_to(source):
    raise RuntimeError("probe did not import pytest from the pinned checkout")
if not (source / "_pytest" / "_version.py").is_file():
    raise RuntimeError("generated version metadata is missing")
result = pytest.main([
    "-q", "-s", "-p", "no:cacheprovider", "--noconftest", str(test_file)
])
sys.exit(int(result))
"""

if Path(sys.executable).resolve() != Path("/usr/bin/python3").resolve():
    raise SystemExit("R17 requires the same /usr/bin/python3 for tests and grading")
if sys.version_info < (3, 10):
    raise SystemExit("R17 requires Python 3.10 or newer")

with tempfile.TemporaryDirectory(prefix="pytest-r17-probe-") as temporary:
    root = Path(temporary)
    source = Path.cwd() / "src"
    overlay = Path.cwd() / ".benchmark_runtime" / "site"
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONNOUSERSITE": "1",
    }

    def invoke(name: str, content: str) -> tuple[int, str]:
        test_file = root / f"{name}.py"
        test_file.write_text(content, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable, "-B", "-S", "-c", CHILD,
                str(source), str(overlay), str(test_file),
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode, result.stdout + result.stderr

    valid_exit, valid_output = invoke("valid_cases", HIDDEN_CASES)
    # Pytest's progress reporter prefixes subsequent captured stdout lines
    # with a dot (e.g. ".R17_OBSERVATION:"). Strip only that progress marker.
    observations = [
        json.loads(line.partition(":")[2])
        for line in valid_output.splitlines()
        if line.lstrip(".").startswith("R17_OBSERVATION:")
    ]
    unknown_exit, unknown_output = invoke("unknown_case", UNKNOWN_CASE)
    print("R17_PROBE_RESULT:" + json.dumps({
        "valid_exit": valid_exit,
        "observations": observations,
        "unknown_exit": unknown_exit,
        "unknown_output": unknown_output[-4000:],
    }, sort_keys=True))
'''

PYTEST_MRO_MARKER_VISIBLE = r'''from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile


VISIBLE_CASES = r"""
import pytest

pytestmark = [
    pytest.mark.priority("module-first"),
    pytest.mark.priority("module-second"),
]

@pytest.mark.priority("root")
@pytest.mark.ancestry("root")
class Root:
    pass

@pytest.mark.priority("left-inner")
@pytest.mark.priority("left-outer")
@pytest.mark.ancestry("left")
class Left(Root):
    pass

@pytest.mark.priority("right")
@pytest.mark.ancestry("right")
class Right(Root):
    pass

@pytest.mark.priority("leaf-inner")
@pytest.mark.priority("leaf-outer")
@pytest.mark.ancestry("leaf")
class TestLeaf(Left, Right):
    @pytest.mark.priority("function-inner")
    @pytest.mark.priority("function-outer")
    def test_marker_priority(self, request):
        priority = [mark.args[0] for mark in request.node.iter_markers("priority")]
        assert priority == [
            "function-outer", "function-inner",
            "leaf-outer", "leaf-inner",
            "left-outer", "left-inner", "right", "root",
            "module-first", "module-second",
        ], repr(priority)
        assert request.node.get_closest_marker("priority").args == ("function-outer",)
        assert [mark.args[0] for mark in request.node.iter_markers("ancestry")] == [
            "leaf", "left", "right", "root",
        ]
        assert request.node.get_closest_marker("ancestry").args == ("leaf",)

@pytest.mark.priority("single-class")
class TestSingle:
    @pytest.mark.priority("single-function")
    def test_single_class_order(self, request):
        assert [mark.args[0] for mark in request.node.iter_markers("priority")] == [
            "single-function", "single-class", "module-first", "module-second",
        ]

def test_unmarked_case_keeps_module_marks(request):
    assert [mark.args[0] for mark in request.node.iter_markers("priority")] == [
        "module-first", "module-second",
    ]
"""

PYTEST_INI = """[pytest]
markers =
    priority(value): marker scope and order
    ancestry(value): class hierarchy order
"""

CHILD = r"""
from importlib import import_module
from pathlib import Path
import sys

source, overlay, test_file, config_file = map(Path, sys.argv[1:5])
source = source.resolve()
overlay = overlay.resolve()
sys.path[:0] = [str(source), str(overlay)]
import pytest
if not Path(pytest.__file__).resolve().is_relative_to(source):
    raise AssertionError("pytest did not import from the pinned source tree")
if pytest.__version__ != "9.2.0.dev0":
    raise AssertionError("pytest generated version metadata differs from the pin")
if not (source / "_pytest" / "_version.py").is_file():
    raise AssertionError("generated pytest version metadata is missing")
for name in ("pluggy", "iniconfig", "pygments", "packaging"):
    module = import_module(name)
    if not Path(module.__file__).resolve().is_relative_to(overlay):
        raise AssertionError(f"{name} did not import from the verified overlay")
result = pytest.main([
    "-q", "-p", "no:cacheprovider", "--noconftest", "-c", str(config_file), str(test_file)
])
sys.exit(int(result))
"""

if Path(sys.executable).resolve() != Path("/usr/bin/python3").resolve():
    raise SystemExit("pytest marker evaluation requires /usr/bin/python3")
if sys.version_info < (3, 10):
    raise SystemExit("pytest marker evaluation requires Python 3.10 or newer")

workspace = Path(__file__).resolve().parent
source = workspace / "src"
overlay = workspace / ".benchmark_runtime" / "site"
with tempfile.TemporaryDirectory(prefix="pytest-mro-visible-") as temporary:
    root = Path(temporary)
    test_file = root / "test_markers.py"
    config_file = root / "pytest.ini"
    test_file.write_text(VISIBLE_CASES, encoding="utf-8")
    config_file.write_text(PYTEST_INI, encoding="utf-8")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root),
        "TMPDIR": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONNOUSERSITE": "1",
    }
    completed = subprocess.run(
        [
            sys.executable, "-B", "-S", "-c", CHILD,
            str(source), str(overlay), str(test_file), str(config_file),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    output = completed.stdout + completed.stderr
    if completed.returncode != 0 or "3 passed" not in output:
        raise AssertionError(f"visible pytest marker cases failed: {output[-1600:]}")

print("PYTEST_MRO_MARKER_VISIBLE_OK: 3 passed")
'''


PYTEST_MRO_MARKER_HIDDEN = r'''from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile


HIDDEN_CASES = r"""
import warnings
import pytest

@pytest.fixture
def root_setup(request):
    request.config._marker_order.setdefault(request.node.nodeid, []).append("root")

@pytest.fixture
def leaf_setup(request):
    request.config._marker_order.setdefault(request.node.nodeid, []).append("leaf")

@pytest.mark.usefixtures("root_setup")
@pytest.mark.parametrize("root_case", ("r0", "r1"), ids=("root0", "root1"))
class ParameterBase:
    pass

@pytest.mark.usefixtures("leaf_setup")
@pytest.mark.parametrize("leaf_case", ("l0", "l1"), ids=("leaf0", "leaf1"))
class TestParameters(ParameterBase):
    def test_inherited_fixture_and_parametrize_order(
        self, root_case, leaf_case, request
    ):
        assert request.config._marker_order[request.node.nodeid] == ["root", "leaf"]
        observed = [
            (item.callspec.params["root_case"], item.callspec.params["leaf_case"])
            for item in request.session.items
            if item.name.split("[", 1)[0] == "test_inherited_fixture_and_parametrize_order"
        ]
        assert observed == [
            ("r0", "l0"), ("r0", "l1"), ("r1", "l0"), ("r1", "l1")
        ]

@pytest.mark.filterwarnings("ignore:marker warning:UserWarning")
class WarningBase:
    pass

@pytest.mark.filterwarnings("error:marker warning:UserWarning")
class TestWarning(WarningBase):
    def test_closest_warning_filter_wins(self):
        with pytest.raises(UserWarning, match="marker warning"):
            warnings.warn("marker warning", UserWarning)

@pytest.mark.rank("root")
class Root:
    pass

@pytest.mark.rank("left")
class Left(Root):
    pass

@pytest.mark.rank("right")
class Right(Root):
    pass

@pytest.mark.rank("leaf")
class TestDynamic(Left, Right):
    @pytest.mark.rank("function")
    def test_plugin_marker_keeps_mro_order(self, request):
        marks = [mark.args[0] for mark in request.node.iter_markers("rank")]
        assert marks == ["function", "plugin", "leaf", "left", "right", "root"]
        assert request.node.get_closest_marker("rank").args == ("function",)
"""

PYTEST_INI = """[pytest]
markers =
    rank(value): dynamic class marker order
"""

CHILD = r"""
from importlib import import_module
from pathlib import Path
import sys

source, overlay, test_file, config_file = map(Path, sys.argv[1:5])
source = source.resolve()
overlay = overlay.resolve()
sys.path[:0] = [str(source), str(overlay)]
import pytest
if not Path(pytest.__file__).resolve().is_relative_to(source):
    raise AssertionError("pytest did not import from the pinned source tree")
if pytest.__version__ != "9.2.0.dev0":
    raise AssertionError("pytest generated version metadata differs from the pin")
if not (source / "_pytest" / "_version.py").is_file():
    raise AssertionError("generated pytest version metadata is missing")
for name in ("pluggy", "iniconfig", "pygments", "packaging"):
    module = import_module(name)
    if not Path(module.__file__).resolve().is_relative_to(overlay):
        raise AssertionError(f"{name} did not import from the verified overlay")

class MarkerPlugin:
    @pytest.hookimpl
    def pytest_configure(self, config):
        config._marker_order = {}

    @pytest.hookimpl
    def pytest_collection_modifyitems(self, session, config, items):
        for item in items:
            if item.name != "test_plugin_marker_keeps_mro_order":
                continue
            class_node = item.parent
            while class_node is not None and class_node.name != "TestDynamic":
                class_node = class_node.parent
            if class_node is None:
                raise AssertionError("collected marker target is missing")
            class_node.add_marker(pytest.mark.rank("plugin"), append=False)

result = pytest.main([
    "-q", "-p", "no:cacheprovider", "--noconftest", "-c", str(config_file), str(test_file)
], plugins=[MarkerPlugin()])
sys.exit(int(result))
"""

if Path(sys.executable).resolve() != Path("/usr/bin/python3").resolve():
    raise SystemExit("pytest marker evaluation requires /usr/bin/python3")
if sys.version_info < (3, 10):
    raise SystemExit("pytest marker evaluation requires Python 3.10 or newer")

workspace = Path.cwd()
source = workspace / "src"
overlay = workspace / ".benchmark_runtime" / "site"
with tempfile.TemporaryDirectory(prefix="pytest-mro-hidden-") as temporary:
    root = Path(temporary)
    test_file = root / "test_marker_holdout.py"
    config_file = root / "pytest.ini"
    test_file.write_text(HIDDEN_CASES, encoding="utf-8")
    config_file.write_text(PYTEST_INI, encoding="utf-8")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root),
        "TMPDIR": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONNOUSERSITE": "1",
    }
    completed = subprocess.run(
        [
            sys.executable, "-B", "-S", "-c", CHILD,
            str(source), str(overlay), str(test_file), str(config_file),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise AssertionError(f"hidden pytest marker cases failed: {output[-1600:]}")

summary = next((line.strip() for line in output.splitlines() if " passed " in line), None)
print("PYTEST_MRO_MARKER_HIDDEN_OK" + (f": {summary}" if summary else ""))
'''


PYTEST_MIXED_INDIRECT_HIDDEN_EXPECTED = json.dumps(
    {
        "valid_exit": 0,
        "observations": [
            ["multiple", ["R0", "chosen-0"], "chosen-0", ["L0", "chosen-0"]],
            ["multiple", ["R1", "chosen-1"], "chosen-1", ["L1", "chosen-1"]],
            ["true", "first=a", "second=x"],
            ["true", "first=b", "second=y"],
            ["false", "direct-1"],
            ["false", "direct-2"],
            ["ordinary", 0],
            ["ordinary", 5],
            ["ordinary", 11],
        ],
        "unknown_exit": 2,
        "unknown_error": "indirect fixture 'absent' doesn't exist",
    },
    separators=(",", ":"),
    sort_keys=True,
)


BEARTYPE_FORWARD_SCOPES_VISIBLE = r"""import sys
from enum import Enum

from beartype import beartype
from beartype._util.cls.utilclsget import get_type_locals
from beartype._util.func.utilfuncframe import get_frame_locals
from beartype.roar import BeartypeCallHintParamViolation

sentinel = object()


def inspect_scope():
    snapshot = get_frame_locals(sys._getframe(1))
    assert isinstance(snapshot, dict) and snapshot['marker'] is sentinel
    snapshot['marker'] = 'modified snapshot'
    snapshot['added_by_probe'] = True


class ClassBody:
    marker = sentinel
    inspect_scope()


assert ClassBody.marker is sentinel
assert 'added_by_probe' not in vars(ClassBody)


class Envelope:
    PENDING = sentinel

    @beartype
    class Status(Enum):
        PENDING = 1
        READY = 2

        def describe(self, count: 'int') -> str:
            return f'{self.name}:{count}'


assert Envelope.PENDING is sentinel
assert Envelope.__new__ is object.__new__
assert isinstance(Envelope(), Envelope)
assert 'describe' not in vars(Envelope) and '_member_map_' not in vars(Envelope)
assert Envelope.Status.READY.describe(3) == 'READY:3'
try:
    Envelope.Status.READY.describe('3')
except BeartypeCallHintParamViolation:
    pass
else:
    raise AssertionError('quoted annotation no longer checks argument types')

namespace = get_type_locals(Envelope.Status)
assert set(namespace) == set(vars(Envelope.Status))
assert namespace['__new__'] is vars(Envelope.Status)['__new__']
print('BEARTYPE_FORWARD_SCOPES_VISIBLE_OK')
"""


BEARTYPE_FORWARD_SCOPES_HIDDEN = r"""import sys
from collections.abc import Mapping
from enum import Enum

from beartype import beartype
from beartype._util.cls.utilclsget import get_type_locals
from beartype._util.func.utilfuncframe import get_frame_locals
from beartype.roar import BeartypeCallHintParamViolation, BeartypeCallHintReturnViolation

marker = object()


def exercise_snapshots(frame):
    first = get_frame_locals(frame)
    second = get_frame_locals(frame)
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first is not second
    assert first['stable'] is marker and second['stable'] is marker
    first['stable'] = 'replacement'
    first['snapshot_only'] = 17
    assert second['stable'] is marker and 'snapshot_only' not in second
    assert frame.f_locals['stable'] is marker
    assert 'snapshot_only' not in frame.f_locals


def probe_caller():
    exercise_snapshots(sys._getframe(1))


# Test an independent module execution namespace, a normal function frame,
# and a custom class namespace. Snapshots are shallow: object identity remains.
module_scope = {'stable': marker, 'probe_caller': probe_caller}
exec('probe_caller()', module_scope)
assert module_scope['stable'] is marker and 'snapshot_only' not in module_scope


def probe_function():
    stable = marker
    probe_caller()
    assert stable is marker


probe_function()


class NamespaceMeta(type):
    @classmethod
    def __prepare__(mcls, name, bases):
        return {}


class Prepared(metaclass=NamespaceMeta):
    stable = marker
    probe_caller()


assert Prepared.stable is marker and 'snapshot_only' not in vars(Prepared)


class Parent:
    inherited_only = marker


class Child(Parent):
    own = marker

    def __new__(cls):
        return object.__new__(cls)

    @staticmethod
    def method():
        return 42


# The helper must keep the complete directly declared namespace, including
# __new__; no requirement on mappingproxy versus another read-only mapping.
for cls in (Parent, Child):
    direct = vars(cls)
    actual = get_type_locals(cls)
    assert isinstance(actual, Mapping)
    assert set(actual) == set(direct)
    assert all(actual[key] is direct[key] for key in direct)
    try:
        actual['probe_injection'] = marker
    except Exception:
        pass  # The task specifies read-only behavior, not an exception class.
    else:
        raise AssertionError('class namespace helper returned a writable mapping')
    assert 'probe_injection' not in actual and 'probe_injection' not in direct
assert '__new__' in get_type_locals(Child)
assert 'inherited_only' not in get_type_locals(Child)


def envelope_new(cls):
    return object.__new__(cls)


class Outer:
    Alias = str
    stable = marker
    __new__ = envelope_new

    @beartype
    class Nested:
        Alias = int
        stable = 99

        def __new__(cls):
            return object.__new__(cls)

        def twice(self, value: 'Alias') -> 'Alias':
            return value * 2

        def bad_return(self, value: 'Alias') -> 'Alias':
            return str(value)


assert Outer.Alias is str and Outer.stable is marker
assert Outer.__new__ is envelope_new
assert isinstance(Outer(), Outer)
assert 'twice' not in vars(Outer) and 'bad_return' not in vars(Outer)
assert Outer.Nested.Alias is int and Outer.Nested.stable == 99
inner = Outer.Nested()
assert inner.twice(7) == 14
try:
    inner.twice('seven')
except BeartypeCallHintParamViolation:
    pass
else:
    raise AssertionError('local class alias no longer validates input')
try:
    inner.bad_return(7)
except BeartypeCallHintReturnViolation:
    pass
else:
    raise AssertionError('local class alias no longer validates return')


def make_family():
    stable = marker

    class Family:
        RED = stable
        BLUE = 'unrelated outer value'

        @beartype
        class Color(Enum):
            RED = 10
            BLUE = 20

            def label(self, text: 'str') -> str:
                return text + self.name

    return Family


family = make_family()
assert family.RED is marker and family.BLUE == 'unrelated outer value'
assert family.__new__ is object.__new__ and isinstance(family(), family)
assert 'label' not in vars(family) and '_member_map_' not in vars(family)
assert family.Color.BLUE.label('paint ') == 'paint BLUE'
try:
    family.Color.BLUE.label(42)
except BeartypeCallHintParamViolation:
    pass
else:
    raise AssertionError('nested enum argument validation lost')
assert set(get_type_locals(family.Color)) == set(vars(family.Color))
print('BEARTYPE_FORWARD_SCOPES_HIDDEN_OK')
"""


MARKDOWN_DEFLIST_VISIBLE = r"""import sys
from html.parser import HTMLParser

import markdown
from markdown.extensions.def_list import DefListExtension


class TextCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)


def render(source):
    return markdown.markdown(source, extensions=[DefListExtension()])


assert render('Term\n: meaning') == '<dl>\n<dt>Term</dt>\n<dd>meaning</dd>\n</dl>'
limit = sys.getrecursionlimit()
try:
    sys.setrecursionlimit(150)
    source = ''.join(f'Term{i:04}X\n: Meaning{i:04}X\n' for i in range(180))
    output = render(source)
    assert sys.getrecursionlimit() == 150
finally:
    sys.setrecursionlimit(limit)
assert '<dl>' in output and output.count('<dl>') == output.count('</dl>')
parser = TextCollector()
parser.feed(output)
words = ' '.join(parser.text).split()
for i in range(180):
    assert words.count(f'Term{i:04}X') == 1
    assert words.count(f'Meaning{i:04}X') == 1
assert render('Term\n: meaning') == '<dl>\n<dt>Term</dt>\n<dd>meaning</dd>\n</dl>'
print('MARKDOWN_DEFLIST_VISIBLE_OK')
"""


MARKDOWN_DEFLIST_HIDDEN = r"""import re
import sys
import xml.etree.ElementTree as ET

import markdown
from markdown.extensions.def_list import DefListExtension


def check_deep(md, count):
    source = ''.join(f'KEY{i:05}Z\n: VAL{i:05}Z\n' for i in range(count))
    before = sys.getrecursionlimit()
    html = md.convert(source)
    assert sys.getrecursionlimit() == before
    root = ET.fromstring('<root>' + html + '</root>')
    assert root.find('dl') is not None
    assert root.find('.//dd') is not None
    actual = re.findall(r'(?:KEY|VAL)\d{5}Z', ' '.join(root.itertext()))
    expected = [token for i in range(count) for token in (f'KEY{i:05}Z', f'VAL{i:05}Z')]
    assert actual == expected, 'input must survive in order without loss or duplication'
    assert root.findall('.//dl'), 'do not disable definition-list parsing'
    md.reset()


def check_shallow(md):
    for source, expected in (
        ('One\n: first\n: second', '<dl><dt>One</dt><dd>first</dd><dd>second</dd></dl>'),
        ('Apple\n: fruit\n\nCarrot\n: vegetable',
         '<dl><dt>Apple</dt><dd>fruit</dd><dt>Carrot</dt><dd>vegetable</dd></dl>'),
        ('Outer\n: description\n\n    Inner\n    : nested',
         '<dl><dt>Outer</dt><dd><p>description</p><dl><dt>Inner</dt><dd>nested</dd></dl></dd></dl>'),
        ('normal paragraph\n\nTerm\n: meaning',
         '<p>normal paragraph</p><dl><dt>Term</dt><dd>meaning</dd></dl>'),
    ):
        actual = re.sub(r'>\s+<', '><', md.convert(source))
        assert actual == expected, (source, actual)
        md.reset()


md = markdown.Markdown(extensions=[DefListExtension()])
check_shallow(md)
# Test under the ordinary interpreter limit and under a smaller caller-owned
# limit. Do not prescribe the cutoff depth or the implementation of the guard.
check_deep(md, 650)
limit = sys.getrecursionlimit()
try:
    for lowered in (120, 180):
        sys.setrecursionlimit(lowered)
        check_deep(md, 240)
        check_shallow(md)
finally:
    sys.setrecursionlimit(limit)
check_shallow(md)
print('MARKDOWN_DEFLIST_HIDDEN_OK')
"""


TASKS = {
    "markdown-definition-list-recursion": RepositoryTask(
        source="python-markdown/markdown",
        upstream="https://github.com/python-markdown/markdown.git",
        base_sha="86d1e46b40673a85b68b75bb88a96d553647d07c",
        reference_fix_sha="26e43de464fb2d682df917d4fcaf4d376f767564",
        editable=frozenset({"markdown/extensions/def_list.py"}),
        visible_script="qa_visible.py",
        visible_source=MARKDOWN_DEFLIST_VISIBLE,
        hidden_source=MARKDOWN_DEFLIST_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            'Repair the definition-list extension in this pinned Python-Markdown '
            'repository. Long runs of adjacent term/definition pairs without blank lines '
            'can cause unbounded recursive block parsing and an uncaught RecursionError, '
            'even for modest input sizes. Make this parsing safe at the current '
            'interpreter recursion limit, including when a caller deliberately uses a '
            'lower limit. Preserve all input content in order when nesting must stop, '
            'allowing excess markers to remain ordinary text rather than dropping '
            'content. Preserve normal shallow definition-list rendering, multiple '
            'definitions, nested lists, ordinary paragraphs, and parser reuse. Do not '
            'change the interpreter recursion limit, disable the extension, truncate '
            'input, or hide crashes by returning incomplete output. You may change only '
            'markdown/extensions/def_list.py. Inspect qa_visible.py and relevant '
            'production code. Do not edit tests, metadata or other files. Run exactly '
            'python3 -B qa_visible.py as the only permitted test command; you may repeat '
            'that exact command after edits. Read-only source inspection commands are '
            'allowed. Do not run other tests or shell commands that modify files.'
        ),
        upstream_pr="https://github.com/python-markdown/markdown/pull/1701",
        license="BSD-3-Clause",
        expected_reference_paths=frozenset({
            "docs/changelog.md",
            "markdown/extensions/def_list.py",
            "tests/test_syntax/extensions/test_def_list.py",
        }),
        require_both_oracles_red_green=True,
        require_each_editable_ablation_red=True,
    ),
    "beartype-forward-reference-namespace-isolation": RepositoryTask(
        source="beartype/beartype",
        upstream="https://github.com/beartype/beartype.git",
        base_sha="aa5b766d9e9bdffdd0c83f7c6b99547c9ede6752",
        reference_fix_sha="bca8aebb579cfd2b0741d0b121ffa4199331c029",
        editable=frozenset({
            "beartype/_util/cls/utilclsget.py",
            "beartype/_util/func/utilfuncframe.py",
        }),
        visible_script="qa_visible.py",
        visible_source=BEARTYPE_FORWARD_SCOPES_VISIBLE,
        hidden_source=BEARTYPE_FORWARD_SCOPES_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            'Repair namespace isolation during quoted forward-reference resolution in '
            'this pinned beartype repository. Decorating an inner class or enum can '
            'overwrite existing attributes in its enclosing class or inject attributes '
            'into that namespace. Preserve outer namespaces and constructors while '
            'retaining correct runtime type checking of quoted annotations, including '
            'aliases declared in the inner class. The internal get_frame_locals helper '
            "must return a fresh mutable dictionary of a frame's local scope without "
            'exposing the live namespace for modification (including class, module and '
            'function frames); preserve the original values and their object identities. '
            'The internal get_type_locals helper must provide a read-only mapping of all '
            'directly declared class attributes, including __new__, without adding '
            'inherited attributes. Do not disable annotation resolution or type checking '
            'to avoid the bug. Inspect qa_visible.py and relevant production code. You '
            'may change only beartype/_util/cls/utilclsget.py and '
            'beartype/_util/func/utilfuncframe.py. Do not edit tests, metadata, or other '
            'files. Run exactly python3 -B qa_visible.py as the only permitted test '
            'command; you may repeat that exact command after edits. Read-only source '
            'inspection commands are allowed. Do not run other tests or shell commands '
            'that modify files.'
        ),
        upstream_pr="https://github.com/beartype/beartype/pull/709",
        license="MIT",
        require_all_editable_changed=False,
        expected_reference_paths=frozenset({
            "beartype/_util/cls/utilclsget.py",
            "beartype/_util/func/utilfuncframe.py",
            "beartype_test/a00_unit/a20_util/func/test_utilfuncframe.py",
            "beartype_test/a00_unit/a60_decor/a60_pep/test_decorpep435.py",
        }),
        require_both_oracles_red_green=True,
        require_each_editable_ablation_red=True,
    ),
    "packaging-legacy-dataclass-pickles": RepositoryTask(
        source="pypa/packaging",
        upstream="https://github.com/pypa/packaging.git",
        base_sha="0efe21aa398df645c259ee31e54a8a4400b1fc6b",
        reference_fix_sha="6c838aca5942572ba04a1d1a603f5eaed0694263",
        editable=frozenset({
            "src/packaging/_structures.py",
            "src/packaging/direct_url.py",
            "src/packaging/pylock.py",
        }),
        visible_script="qa_visible.py",
        visible_source=PACKAGING_LEGACY_DATACLASS_VISIBLE,
        hidden_source=PACKAGING_LEGACY_DATACLASS_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Repair backward compatibility for old serialized dataclass values "
            "in this pinned pypa/packaging repository. The older 26.3 version "
            "serialized pylock and direct_url dataclasses using dictionary "
            "instance state; current frozen slotted dataclasses can silently "
            "restore those dictionary keys as field values rather than the "
            "original data. Deserialize authentic old pickle values accurately "
            "across the supported nested model types, reject structurally "
            "invalid states instead of silently mis-restoring fields, and "
            "preserve round trips and the existing pickle format of current "
            "slotted dataclasses. Inspect qa_visible.py and the relevant "
            "production code. You may change only "
            "src/packaging/_structures.py, src/packaging/direct_url.py, "
            "and src/packaging/pylock.py. Do not edit tests, packaging metadata, "
            "or any other file. Run exactly python3 -B qa_visible.py using "
            "run_command to verify the fix; no other commands are authorized."
        ),
        upstream_pr="https://github.com/pypa/packaging/pull/1548",
        license="Apache-2.0 OR BSD-2-Clause",
        require_all_editable_changed=False,
        require_both_oracles_red_green=True,
        expected_reference_paths=frozenset({
            "pyproject.toml",
            "src/packaging/_structures.py",
            "src/packaging/direct_url.py",
            "src/packaging/pylock.py",
            "tests/test_direct_url.py",
            "tests/test_pylock.py",
        }),
    ),
    "pyflakes-functional-typing-fields": RepositoryTask(
        source="PyCQA/pyflakes",
        upstream="https://github.com/PyCQA/pyflakes.git",
        base_sha="9f0a7f9c8feb4d086a918a708bb778b38ce7a248",
        reference_fix_sha="5f2af952d81a8bead32d3ab91a60f7efbc6d8aea",
        editable=frozenset({"pyflakes/checker.py"}),
        visible_script="qa_visible.py",
        visible_source=PYFLAKES_TYPING_FIELDS_VISIBLE,
        hidden_source=PYFLAKES_TYPING_FIELDS_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Repair a historical static analysis bug in this pinned Pyflakes "
            "repository. Functional typing.TypedDict and typing.NamedTuple "
            "calls can report a false unused import when their second "
            "argument is a runtime expression that uses an imported name. "
            "Check that expression for ordinary name uses and undefined "
            "names. Preserve annotation handling for types in literal "
            "TypedDict mappings and NamedTuple field lists, including "
            "quoted forward references. Inspect qa_visible.py and the "
            "relevant source. Edit only pyflakes/checker.py, then run exactly "
            "python3 -B qa_visible.py with run_command. No other commands are "
            "authorized. Preserve upstream tests and all other files."
        ),
    ),
    "click-clirunner-context-settings": RepositoryTask(
        source="pallets/click",
        upstream="https://github.com/pallets/click.git",
        base_sha="ac0e297ec2d7d78455a7d18ce306996e668c4a85",
        reference_fix_sha="90a2973f85fffc4d8a20effb2bcdff3f394c5e62",
        editable=frozenset({"src/click/testing.py"}),
        visible_script="qa_visible.py",
        visible_source=CLICK_CONTEXT_SETTINGS_VISIBLE,
        hidden_source=CLICK_CONTEXT_SETTINGS_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Add a backward-compatible feature to Click's CliRunner.invoke in "
            "this pinned repository: accept an optional context_settings "
            "mapping that passes Context settings to the command's root "
            "context. A caller must be able to set context.color independently "
            "of CliRunner.invoke(color=...) for output capture, and explicit "
            "context settings must override the command's declared context "
            "defaults. Preserve existing extra keyword arguments, no-override "
            "invocations, capture semantics, and the input mapping. Inspect "
            "qa_visible.py and relevant source, edit ONLY src/click/testing.py, "
            "then run exactly python3 -B qa_visible.py with run_command. "
            "No other commands are authorized. Do not modify tests, docs, "
            "or metadata."
        ),
    ),
    "toolz-partition-invalid-length": RepositoryTask(
        source="pytoolz/toolz",
        upstream="https://github.com/pytoolz/toolz.git",
        base_sha="08f2604f599847272d801c7f0467451de59fa33a",
        reference_fix_sha="defe68c33ddaa2c2a9b3d4e53818a1fbe63c3f85",
        editable=frozenset({"toolz/itertoolz.py"}),
        visible_script="qa_visible.py",
        visible_source=TOOLZ_PARTITION_LENGTH_VISIBLE,
        hidden_source=TOOLZ_PARTITION_LENGTH_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Fix a historical correctness bug in this pinned toolz repository. "
            "When an iterable with a __len__ reports a value inconsistent "
            "with the items it yields, partition_all may silently truncate "
            "or mispartition a partial trailing chunk. Detect mismatched "
            "reported length for partially filled partitions and raise "
            "LookupError rather than returning incorrect data. Preserve "
            "correct partitioning of normal sized sequences, generators, "
            "iterables whose __len__ raises TypeError, full chunks, and "
            "empty inputs. Inspect qa_visible.py and the existing code, "
            "edit ONLY toolz/itertoolz.py, then run exactly "
            "python3 -B qa_visible.py with run_command. No other commands "
            "are authorized. Do not alter tests, docs or metadata."
        ),
    ),
    "moreitertools-peekable-reverse-slice": RepositoryTask(
        source="more-itertools/more-itertools",
        upstream="https://github.com/more-itertools/more-itertools.git",
        base_sha="4aeb4338ae1407d8da9723e86da99efeb446f719",
        reference_fix_sha="69dbc3392928afe1cc892a387bf0e721522f21c2",
        editable=frozenset({"more_itertools/more.py"}),
        visible_script="qa_visible.py",
        visible_source=MORE_ITERTOOLS_PEEKABLE_VISIBLE,
        hidden_source=MORE_ITERTOOLS_PEEKABLE_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Repair a lazy-iterator bug in this pinned more-itertools repository. "
            "A peekable reverse slice with a nonnegative start, negative step "
            "and omitted stop (for example items[5::-1]) currently tries to "
            "exhaust the entire input. This hangs on an infinite iterator and "
            "unnecessarily buffers a large finite one. Fetch only the needed "
            "prefix while preserving Python slice results, existing cached "
            "items, normal subsequent iteration, forward slicing, and genuine "
            "right-relative negative indices or an omitted reverse start. "
            "Inspect qa_visible.py and the peekable implementation; edit ONLY "
            "more_itertools/more.py. Run exactly python3 -B qa_visible.py "
            "with run_command after editing. No other commands are authorized. "
            "Do not modify tests, documentation, metadata, or other files."
        ),
    ),
    "rich-soft-wrap-background-style": RepositoryTask(
        source="Textualize/rich",
        upstream="https://github.com/Textualize/rich.git",
        base_sha="05ff970926cbd0b5d0acb6fc18cdbd1fc971e582",
        reference_fix_sha="2c247e6da72dfa69dd6148f1a0657e2440d1ebe9",
        editable=frozenset({"rich/console.py", "rich/segment.py"}),
        visible_script="qa_visible.py",
        visible_source=RICH_SOFT_WRAP_VISIBLE,
        hidden_source=RICH_SOFT_WRAP_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Repair a real Rich terminal output bug in this pinned repository. "
            "When Console.print() is given a background-colored style and "
            "soft_wrap=True, ANSI styling can leak across a newline: physical "
            "linefeeds are styled or reset only after the newline, so the "
            "next terminal line inherits a background. Preserve visible "
            "character styles but make line breaks unstyled, including "
            "multiline text and blank lines, without breaking existing "
            "unstyled printing, non-soft-wrapped output or styled Text. "
            "Investigate the render/segment handling in rich/console.py and "
            "rich/segment.py, inspect qa_visible.py, and edit ONLY "
            "rich/console.py and rich/segment.py. Run exactly "
            "python3 -B qa_visible.py with run_command after editing. "
            "No other commands are authorized. Do not change tests, "
            "metadata, docs, or unrelated files."
        ),
    ),
    "boltons-bulk-ordered-collections": RepositoryTask(
        source="mahmoud/boltons",
        upstream="https://github.com/mahmoud/boltons.git",
        base_sha="e66cade323f5c11cebb4bd0f099e634b245adccd",
        reference_fix_sha="a82b55af744fec5ac38b9f6b8ae24bb937b22666",
        editable=frozenset({"boltons/listutils.py", "boltons/setutils.py"}),
        visible_script="qa_visible.py",
        visible_source=BOLTONS_BULK_VISIBLE,
        hidden_source=BOLTONS_BULK_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Repair the ordered-collection behavior in this pinned boltons "
            "repository while preserving public APIs. Two modules are involved. "
            "In boltons/listutils.py, multi-sublists BarrelList should raise "
            "IndexError for out-of-range reads, clamp insert indexes just as "
            "Python list does at both positive and negative extremes, and "
            "allow ordinary pop() after its tail sublist is drained by "
            "indexed pops. In boltons/setutils.py, IndexedSet bulk "
            "difference_update and intersection_update must preserve "
            "insertion order, member lookup and index consistency without "
            "repeated O(n) compaction on thousands of scattered removals. "
            "A single linear-time rebuild for large changes is acceptable, "
            "with existing incremental behavior for smaller updates. "
            "Inspect qa_visible.py and the two implementation files. Edit "
            "ONLY boltons/listutils.py and boltons/setutils.py, and run "
            "exactly python3 -B qa_visible.py with run_command afterward. "
            "No other commands are authorized. Do not modify tests, "
            "documentation, metadata or unrelated files."
        ),
    ),
    "packaging-exception-pickle": RepositoryTask(
        source="pypa/packaging",
        upstream="https://github.com/pypa/packaging.git",
        base_sha="458b0ae53e5a4261df851657d147e590b4546eb2",
        reference_fix_sha="1a9c4cb6b6aac7fb6137fcdc4f440af912d8d87f",
        editable=frozenset({
            "src/packaging/metadata.py",
            "src/packaging/dependency_groups.py",
        }),
        visible_script="qa_visible.py",
        visible_source=PACKAGING_PICKLE_VISIBLE,
        hidden_source=PACKAGING_PICKLE_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Fix an exception serialization compatibility defect in this "
            "pinned Python packaging repository. Two public ValueError "
            "subclasses, InvalidMetadata and CyclicDependencyGroup, store "
            "constructor fields but pass only a formatted error message to "
            "ValueError. As a result, Python pickle and copy round trips "
            "cannot reconstruct them from .args. Make both exceptions "
            "round-trip safely, preserving each class, all constructor "
            "fields, human-readable message, and ordinary exception behavior. "
            "Review qa_visible.py and the existing class definitions; edit "
            "ONLY src/packaging/metadata.py and "
            "src/packaging/dependency_groups.py. Execute exactly "
            "python3 -B qa_visible.py with run_command afterward. "
            "No other commands are authorized. Do not change tests, metadata, "
            "or other public APIs."
        ),
    ),
    "attrs-generator-setattr-hooks": RepositoryTask(
        source="python-attrs/attrs",
        upstream="https://github.com/python-attrs/attrs.git",
        base_sha="d3a98cf05ab15769003dcca3eae91cdd6504b83b",
        reference_fix_sha="4b5b295bb815bf845fa3570bf63781a88212db40",
        editable=frozenset({"src/attr/_compat.py", "src/attr/_make.py"}),
        visible_script="qa_visible.py",
        visible_source=ATTRS_GENERATOR_VISIBLE,
        hidden_source=ATTRS_GENERATOR_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Implement generator-based attribute-setter hooks in this pinned attrs "
            "repository. Currently on_setattr hooks can return a transformed "
            "value but cannot be generator functions. Support an on_setattr "
            "generator that yields exactly one replacement value: execute it "
            "up to the yield, assign the yielded value, then resume it so its "
            "post-assignment cleanup can run. Propagate zero-yield failures, "
            "reject hooks that yield twice, and close such generators. Preserve "
            "ordinary callable setters, attributes exempted with NO_OP, class "
            "and field hooks, and both attrs.define and attr.s callers. "
            "Put the reusable, lazily cached generator-function classifier in "
            "src/attr/_compat.py and connect it to generated attribute writes "
            "in src/attr/_make.py. Review qa_visible.py, edit ONLY those two "
            "source files, then run exactly python3 -B qa_visible.py with "
            "run_command. No other commands are authorized. Preserve all "
            "public signatures and other files."
        ),
    ),
    "tomlkit-comment-control-validation": RepositoryTask(
        source="python-poetry/tomlkit",
        upstream="https://github.com/python-poetry/tomlkit.git",
        base_sha="55d07680cabf023885d511d2bf2000963e4e3dc7",
        reference_fix_sha="7e0016217ea2dbc369fc721acfd8ace52a899a99",
        editable=frozenset(
            {
                "tomlkit/_utils.py",
                "tomlkit/api.py",
                "tomlkit/items.py",
            }
        ),
        visible_script="qa_visible.py",
        visible_source=TOMLKIT_COMMENT_VISIBLE,
        hidden_source=TOMLKIT_COMMENT_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Fix invalid TOML comment generation in this pinned python-poetry/tomlkit "
            "repository. The public comment-writing APIs currently allow forbidden "
            "ASCII control characters into serialized TOML, so the output can no "
            "longer be parsed. Centralize character validation in "
            "tomlkit/_utils.py and apply it consistently to tomlkit.comment(), "
            "Item.comment(), and Array.add_line(comment=...). Reject control "
            "characters U+0000–U+001F except TAB and permitted line endings, plus "
            "DEL U+007F. Standalone comments support LF and CRLF line endings but "
            "must reject bare CR; attached comments are single-line and must "
            "reject LF and CR. Preserve valid tabs and Unicode, preserve existing "
            "document content when a comment is rejected, and retain all other "
            "formatting behavior. Edit ONLY tomlkit/_utils.py, tomlkit/api.py, "
            "and tomlkit/items.py. Review qa_visible.py and run exactly "
            "python3 -B qa_visible.py with run_command afterward. No other "
            "commands are authorized."
        ),
    ),
    "pluggy-role-module-split": RepositoryTask(
        source="pytest-dev/pluggy",
        upstream="https://github.com/pytest-dev/pluggy.git",
        base_sha="d70fe1840d01c78b7d85f693cfef01ccdf2f80a6",
        reference_fix_sha="17d4ee957ffe86e5e65f892fef8898bb2a386d6e",
        editable=frozenset(
            {
                "src/pluggy/_caller.py",
                "src/pluggy/_callers.py",
                "src/pluggy/_config.py",
                "src/pluggy/_decorators.py",
                "src/pluggy/_execution.py",
                "src/pluggy/_hooks.py",
                "src/pluggy/_impl.py",
            }
        ),
        visible_script="qa_visible.py",
        visible_source=PLUGGY_MODULE_SPLIT_VISIBLE,
        hidden_source=PLUGGY_MODULE_SPLIT_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Refactor the internal hook implementation in this pinned "
            "pytest-dev/pluggy repository by splitting the monolithic "
            "pluggy._hooks and pluggy._callers modules into role-specific "
            "modules, without changing runtime behavior. Move hook callers "
            "and relay types into src/pluggy/_caller.py; option TypedDicts "
            "and normalization into src/pluggy/_config.py; marker decorators, "
            "HookSpec and varnames into src/pluggy/_decorators.py; multicall "
            "and wrapper/teardown execution into src/pluggy/_execution.py; "
            "and HookImpl plus its type aliases into src/pluggy/_impl.py. "
            "Keep src/pluggy/_hooks.py and src/pluggy/_callers.py as "
            "backward-compatible re-export shims for every existing public "
            "and private import, preserving object identity rather than "
            "creating duplicate implementations. Preserve top-level pluggy "
            "exports, hook ordering, firstresult, historic replay, subset "
            "callers, old-style hookwrappers and new-style wrappers. This is "
            "a code-organization refactor, not a feature change. Edit ONLY "
            "src/pluggy/_caller.py, _callers.py, _config.py, _decorators.py, "
            "_execution.py, _hooks.py, and _impl.py. Review qa_visible.py "
            "and run exactly python3 -B qa_visible.py with run_command "
            "afterward. No other commands are authorized. Do not change "
            "tests, changelog, packaging metadata, or any other files."
        ),
    ),
    "dateutil-strict-numeric-fields": RepositoryTask(
        source="dateutil/dateutil",
        upstream="https://github.com/dateutil/dateutil.git",
        base_sha="6e1864c7c309f4d905e8b337864ca8b5d3ff9d34",
        reference_fix_sha="6f1362220007b9d868638630043636e0f3285a3f",
        editable=frozenset({
            "src/dateutil/parser/isoparser.py",
            "src/dateutil/tz/tz.py",
        }),
        visible_script="qa_visible.py",
        visible_source=DATEUTIL_NUMERIC_VISIBLE,
        hidden_source=DATEUTIL_NUMERIC_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Repair a historical strict-numeric-input bug in this pinned "
            "python-dateutil repository. ISO-8601 date/time parsing uses "
            "int() on fixed-width fields, which incorrectly accepts signs, "
            "underscores, whitespace and truncated ASCII digit fields. "
            "The calendar tzical parser similarly permits malformed "
            "TZOFFSETFROM/TZOFFSETTO values, including non-ASCII digits. "
            "Require exact-width ASCII decimal digits where applicable "
            "without changing valid ISO week/ordinal dates, valid date-time "
            "offsets, valid calendar timezone values or public APIs. "
            "Inspect qa_visible.py and both implicated modules; edit ONLY "
            "src/dateutil/parser/isoparser.py and src/dateutil/tz/tz.py, "
            "then run exactly python3 -B qa_visible.py with run_command. "
            "No other commands are authorized. Do not edit tests or metadata."
        ),
    ),
    "click-resource-exception": RepositoryTask(
        source="pallets/click",
        upstream="https://github.com/pallets/click.git",
        base_sha="16fe802a3f96c4c8fa3cd382f1a7577fda0c5321",
        reference_fix_sha="555fa9bb37770a6845a98be60b0c84876775552e",
        editable=frozenset({"src/click/core.py"}),
        visible_script="qa_visible.py",
        visible_source=CLICK_RESOURCE_VISIBLE,
        hidden_source=CLICK_RESOURCE_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Repair a historical lifecycle bug in this pinned Click CLI framework. "
            "Context.with_resource() registers context managers for cleanup, "
            "but when a command's context exits with an exception, managed "
            "resources are currently exited as if the operation succeeded. "
            "Forward the real exception type, value, and traceback through "
            "resource cleanup. Respect context managers that suppress an "
            "exception, preserve LIFO resource order, ordinary successful "
            "cleanup, reusable contexts, and the public Context interface. "
            "Inspect the code and qa_visible.py, edit ONLY src/click/core.py, "
            "then execute exactly python3 -B qa_visible.py with run_command. "
            "No other commands are authorized. Do not modify tests or metadata."
        ),
    ),
    "h11-chunk-footer": RepositoryTask(
        source="python-hyper/h11",
        upstream="https://github.com/python-hyper/h11.git",
        base_sha="31e626c64e1e28db3cd73a6aa0ac057f1b915c18",
        reference_fix_sha="dff7cc397a26ed4acdedd92d1bda6c8f18a6ed9f",
        editable=frozenset({"h11/_readers.py"}),
        visible_script="qa_visible.py",
        visible_source=H11_VISIBLE,
        hidden_source=H11_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Repair a real HTTP/1.1 chunked-body parser bug in this pinned h11 "
            "repository. After a nonempty chunk's data, the parser currently "
            "discards two arbitrary bytes; it must reject any chunk footer "
            "that is not exactly CRLF, including when the two bytes arrive "
            "in separate reads. Preserve successful incremental decoding, "
            "public APIs, and existing valid behavior. Examine the source and "
            "qa_visible.py, change only h11/_readers.py, and run exactly "
            "`python3 -B qa_visible.py` using run_command. No other commands "
            "are authorized. Do not modify tests, metadata, or other files."
        ),
    ),
    "urllib3-header-sanitization": RepositoryTask(
        source="urllib3/urllib3",
        upstream="https://github.com/urllib3/urllib3.git",
        base_sha="8883fd900057ee65f553586389ecd70269f05e04",
        reference_fix_sha="96449eb1ac8cfca8fb9605b8bbff7a5fe0081dd2",
        editable=frozenset({"src/urllib3/connection.py"}),
        visible_script="qa_visible.py",
        visible_source=URLLIB3_VISIBLE,
        hidden_source=URLLIB3_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Fix a real HTTP response header normalization defect in this "
            "pinned urllib3 repository. In src/urllib3/connection.py, "
            "the existing header-value normalizer handles obsolete CRLF "
            "folding, but can still leave raw bare CR, bare LF, or NUL "
            "characters and leading/trailing SP or HTAB in header values. "
            "Normalize these safely while preserving ordinary text and "
            "obsolete folding semantics. Inspect the source and qa_visible.py, "
            "change only src/urllib3/connection.py, and run exactly "
            "python3 -B qa_visible.py with run_command. No other commands "
            "are authorized. Preserve all public APIs and other files."
        ),
    ),
    "urllib3-proxy-redirect": RepositoryTask(
        source="urllib3/urllib3",
        upstream="https://github.com/urllib3/urllib3.git",
        base_sha="e349d97f2bf234b95d5dd9e689d056a521dfc3b0",
        reference_fix_sha="01cd60b539f808a65e9c5aa93db1c48292432176",
        editable=frozenset(
            {
                "src/urllib3/connectionpool.py",
                "src/urllib3/poolmanager.py",
            }
        ),
        visible_script="qa_visible.py",
        visible_source=URLLIB3_REDIRECT_VISIBLE,
        hidden_source=URLLIB3_REDIRECT_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Repair a historical proxy redirect bug in this pinned urllib3 "
            "repository. Redirect origin comparison must use the original "
            "request URL's origin rather than the connection pool's proxy host. "
            "On a same-origin redirect, preserve Authorization and Cookie; "
            "on a cross-origin redirect, strip sensitive redirect headers "
            "and remove any previously set Host so the next request computes "
            "its destination Host correctly. Keep scheme, normalized host "
            "and default-port comparison consistent across the connection "
            "pool and redirect manager via a shared internal comparison helper. "
            "Inspect qa_visible.py and both modules; edit ONLY "
            "src/urllib3/connectionpool.py and src/urllib3/poolmanager.py, "
            "then run exactly python3 -B qa_visible.py with run_command. "
            "No other commands are authorized. Preserve public APIs and tests."
        ),
    ),
    "urllib3-url-auth-feature": RepositoryTask(
        source="urllib3/urllib3",
        upstream="https://github.com/urllib3/urllib3.git",
        base_sha="5454903f272718f19d68d5f78789251bd5c5b443",
        reference_fix_sha="5cf0247049a4baa02e830666de53431e5b575694",
        editable=frozenset({"src/urllib3/util/url.py"}),
        visible_script="qa_visible.py",
        visible_source=URLLIB3_AUTH_FEATURE_VISIBLE,
        hidden_source=URLLIB3_AUTH_FEATURE_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Implement a new URL authentication convenience feature in this "
            "pinned real urllib3 repository. The Url result of parse_url() "
            "currently stores its userinfo in percent-encoded auth. Add "
            "read-only Url.auth_decoded returning (username, password) with "
            "percent escapes decoded as UTF-8. Without auth return (None, None); "
            "without a password return (username, None), distinguishing an "
            "empty password. Add Url.auth_decoded_joined returning the decoded "
            "userinfo as a Basic-auth-ready colon-separated string. Include "
            "a trailing colon for a username without a password, and return "
            "None when no auth is supplied. Partition on the first raw colon "
            "before decoding, so encoded colons are preserved correctly. "
            "Retain the existing raw Url.auth value and other URL behavior. "
            "Review qa_visible.py; edit ONLY src/urllib3/util/url.py, then "
            "execute exactly python3 -B qa_visible.py with run_command. "
            "No other commands are authorized. Preserve other files and APIs."
        ),
    ),
    "pytest-mixed-indirect-fixture-precedence": RepositoryTask(
        source="pytest-dev/pytest",
        upstream="https://github.com/pytest-dev/pytest.git",
        base_sha="fd47511afeb024d2fbad9257d1b44310672c6a0f",
        reference_fix_sha="6b6502e94b78b41951b70265a84187a419e8ce8c",
        editable=frozenset({"src/_pytest/fixtures.py", "src/_pytest/python.py"}),
        visible_script="qa_visible.py",
        visible_source=PYTEST_MIXED_INDIRECT_VISIBLE,
        hidden_source=PYTEST_MIXED_INDIRECT_HIDDEN,
        hidden_expected_json=PYTEST_MIXED_INDIRECT_HIDDEN_EXPECTED,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Repair mixed direct and indirect parameter handling in this "
            "pinned pytest repository. When a parametrized test marks only "
            "selected arguments as indirect, the remaining arguments must "
            "retain their direct values even if a fixture with the same name "
            "declares its own parameters. Preserve fixture setup and dependency "
            "capture, argument ordering, indirect=True and indirect=False, "
            "ordinary parametrization, and a clear collection error when an "
            "indirect fixture name does not exist. Inspect qa_visible.py and "
            "the relevant implementation. Edit only "
            "src/_pytest/fixtures.py and src/_pytest/python.py as needed, then "
            "run exactly python3 -B qa_visible.py with run_command. No other "
            "commands are authorized. Do not modify tests, documentation, "
            "metadata, or other files."
        ),
        runtime=PYTEST_R17_RUNTIME,
        upstream_pr="https://github.com/pytest-dev/pytest/pull/13976",
        license="MIT",
        require_all_editable_changed=False,
        expected_reference_paths=frozenset(
            {
                "src/_pytest/fixtures.py",
                "src/_pytest/python.py",
                "testing/python/collect.py",
                "testing/python/metafunc.py",
            }
        ),
    ),
    "pytest-mro-marker-precedence": RepositoryTask(
        source="pytest-dev/pytest",
        upstream="https://github.com/pytest-dev/pytest.git",
        base_sha="6665a79fb9a6553ce4e1be5574c21a9e3b769eec",
        reference_fix_sha="9255aa01e48f47be636987293471a5b102d78c26",
        editable=frozenset(
            {
                "src/_pytest/compat.py",
                "src/_pytest/fixtures.py",
                "src/_pytest/mark/structures.py",
                "src/_pytest/nodes.py",
                "src/_pytest/python.py",
                "src/_pytest/warnings.py",
            }
        ),
        visible_script="qa_visible.py",
        visible_source=PYTEST_MRO_MARKER_VISIBLE,
        hidden_source=PYTEST_MRO_MARKER_HIDDEN,
        command_grant="/usr/bin/python3 -B qa_visible.py",
        prompt=(
            "Repair marker precedence for inherited test classes in this "
            "pinned pytest repository. For tests in a class hierarchy, "
            "iter_markers() must list the closest class before its bases in "
            "Python MRO order, and get_closest_marker() must return the "
            "closest matching class marker when the test function has no "
            "marker with that name. Preserve function, class, and module "
            "priorities and stacked decorator order. Preserve base-first "
            "behavior for inherited usefixtures and parametrize marks, "
            "warning-filter precedence, and markers added by plugins during "
            "collection. Inspect qa_visible.py and the relevant implementation. "
            "Edit only these paths as needed: src/_pytest/compat.py, "
            "src/_pytest/fixtures.py, src/_pytest/mark/structures.py, "
            "src/_pytest/nodes.py, src/_pytest/python.py, and "
            "src/_pytest/warnings.py. Run exactly "
            "/usr/bin/python3 -B qa_visible.py with run_command. No other "
            "commands are authorized. Do not modify tests, documentation, "
            "changelogs, or metadata."
        ),
        runtime=PYTEST_MRO_MARKER_RUNTIME,
        upstream_pr="https://github.com/pytest-dev/pytest/pull/14630",
        license="MIT",
        require_all_editable_changed=False,
        expected_reference_paths=frozenset(
            {
                "AUTHORS",
                "changelog/10406.bugfix.rst",
                "changelog/14329.bugfix.rst",
                "changelog/14630.improvement.rst",
                "doc/en/how-to/capture-warnings.rst",
                "src/_pytest/compat.py",
                "src/_pytest/fixtures.py",
                "src/_pytest/mark/structures.py",
                "src/_pytest/nodes.py",
                "src/_pytest/python.py",
                "src/_pytest/warnings.py",
                "testing/test_mark.py",
                "testing/test_skipping.py",
                "testing/test_warnings.py",
            }
        ),
        require_both_oracles_red_green=True,
    ),
}


TASKS["packaging-legacy-dataclass-pickles-r23b"] = replace(
    TASKS["packaging-legacy-dataclass-pickles"],
    hidden_source=_packaging_r23b_hidden_source(),
    prompt=(
        "Repair backward compatibility for old serialized dataclass values "
        "in this pinned pypa/packaging repository. The older 26.3 version "
        "serialized pylock and direct_url dataclasses using dictionary "
        "instance state; current frozen slotted dataclasses can silently "
        "restore those dictionary keys as field values rather than the "
        "original data. Deserialize authentic old pickle values accurately "
        "across the supported nested model types. Reject malformed dict and "
        "list states instead of silently mis-restoring fields, while accepting "
        "either TypeError or ValueError for malformed-state rejection. Preserve "
        "round trips and the existing pickle format of current slotted "
        "dataclasses. Inspect qa_visible.py and the relevant production code. "
        "You may change only src/packaging/_structures.py, "
        "src/packaging/direct_url.py, and src/packaging/pylock.py. Do not edit "
        "tests, packaging metadata, or any other file. Run exactly "
        "python3 -B qa_visible.py as the only permitted test command; "
        "you may repeat that exact command after edits. Read-only source "
        "inspection commands are allowed. Do not run other tests or shell "
        "commands that modify files."
    ),
    require_each_editable_ablation_red=True,
    diagnostic_retake_of="packaging-legacy-dataclass-pickles",
)


def task_evidence_classification(task: RepositoryTask) -> str:
    if task.diagnostic_retake_of is not None:
        return "diagnostic-retake-same-upstream-source-not-fresh-held-out"
    return "source-backed-task"


def shared_user_prompt(task: RepositoryTask) -> str:
    """Model-neutral task input for paired Ash/plain-Codex evaluations."""
    return (
        task.prompt.replace("using run_command", "using the available command tool")
        .replace("with run_command", "using the available command tool")
        .replace(
            "No other commands are authorized.",
            "Read-only source inspection commands are allowed. "
            "Do not run other tests or shell commands that modify files.",
        )
    )


def fetch_pinned(destination: Path, task: RepositoryTask, sha: str) -> None:
    """Clone a constant, exact-commit public source; never infer HEAD from a branch."""
    destination.mkdir()
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(destination.parent),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for args in (
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", task.upstream],
        ["git", "-c", "protocol.version=2", "fetch", "--depth=1", "origin", sha],
        ["git", "-c", "core.hooksPath=/dev/null", "checkout", "-q", "--detach", sha],
    ):
        result = command(args, cwd=destination, env=environment, timeout=45)
        if result.returncode:
            raise RuntimeError(f"pinned upstream checkout failed: {args[1]}")
    result = command(
        ["git", "rev-parse", "HEAD"], cwd=destination, env=environment, timeout=5
    )
    if result.returncode or result.stdout.strip() != sha:
        raise RuntimeError("pinned upstream checkout does not match manifest commit")


def check_edits(
    original: dict[str, str],
    updated: dict[str, str],
    editable: frozenset[str],
    *,
    require_all_editable_changed: bool = True,
) -> tuple[bool, bool]:
    if UNSAFE_ENTRIES.intersection(original.values()) or UNSAFE_ENTRIES.intersection(updated.values()):
        return False, False
    changed_paths = {
        path
        for path in original.keys() | updated.keys()
        if original.get(path) != updated.get(path)
    }
    unexpected_path_changes = (original.keys() ^ updated.keys()) - editable
    if unexpected_path_changes:
        return False, False
    # Editable paths may be new or intentionally removed by a refactor.
    # Every other addition, deletion, or content change remains protected.
    protected = changed_paths.issubset(editable)
    content_changed = {
        path
        for path in editable
        if content_identity(original.get(path)) != content_identity(updated.get(path))
    }
    changed = bool(editable) and bool(content_changed)
    if require_all_editable_changed:
        changed = changed and content_changed == editable
    return protected, changed


_RUNTIME_MAX_FILES = 1024
_RUNTIME_MAX_BYTES = 64 * 1024 * 1024


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _real_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise RuntimeError(
            f"required runtime directory is unavailable: {path.name}"
        ) from error
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(f"runtime path is not a plain directory: {path.name}")


def _locked_runtime_versions(
    runtime: PythonSourceRuntime,
) -> tuple[
    bytes,
    Path,
    dict[str, tuple[RuntimePackage, dict[str, tuple[str, str]]]],
]:
    """Resolve the active Ash venv and exact lock-approved purelib sources."""
    prefix = Path(sys.prefix).resolve(strict=True)
    base_prefix = Path(sys.base_prefix).resolve(strict=True)
    if prefix == base_prefix or prefix.name != ".venv":
        raise RuntimeError("R17 runtime source must come from an active Ash virtualenv")
    pyvenv_cfg = prefix / "pyvenv.cfg"
    try:
        cfg_mode = pyvenv_cfg.lstat().st_mode
    except OSError as error:
        raise RuntimeError("active Ash virtualenv metadata is missing") from error
    if stat.S_ISLNK(cfg_mode) or not stat.S_ISREG(cfg_mode):
        raise RuntimeError("active Ash virtualenv metadata is unsafe")

    purelib_text = sysconfig.get_paths().get("purelib")
    if not isinstance(purelib_text, str):
        raise RuntimeError("active Python has no purelib directory")
    purelib = Path(purelib_text).resolve(strict=True)
    if not _is_within(purelib, prefix):
        raise RuntimeError(
            "runtime source directory is outside the active Ash virtualenv"
        )
    _real_directory(purelib)

    lock_path = REPO / "uv.lock"
    try:
        lock_bytes = lock_path.read_bytes()
        lock = tomllib.loads(lock_bytes.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise RuntimeError(
            "R17 dependency lockfile is unavailable or invalid"
        ) from error
    lock_packages = lock.get("package")
    if not isinstance(lock_packages, list):
        raise RuntimeError("R17 dependency lockfile has no package table")

    package_metadata: dict[str, tuple[RuntimePackage, dict[str, tuple[str, str]]]] = {}
    for package in runtime.packages:
        matching = [
            item
            for item in lock_packages
            if isinstance(item, dict)
            and str(item.get("name", "")).casefold() == package.distribution.casefold()
        ]
        if len(matching) != 1 or matching[0].get("version") != package.version:
            raise RuntimeError(
                f"R17 lock does not uniquely pin {package.distribution} {package.version}"
            )
        try:
            distribution = importlib.metadata.distribution(package.distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimeError(
                f"trusted runtime package is missing: {package.distribution}"
            ) from error
        metadata_path = Path(getattr(distribution, "_path", ""))
        _real_directory(metadata_path)
        resolved_metadata = metadata_path.resolve(strict=True)
        if not _is_within(resolved_metadata, purelib):
            raise RuntimeError(
                f"runtime metadata is outside the active Ash virtualenv: {package.distribution}"
            )
        metadata_file = metadata_path / "METADATA"
        record_file = metadata_path / "RECORD"
        for metadata_entry in (metadata_file, record_file):
            try:
                metadata_mode = metadata_entry.lstat().st_mode
            except OSError as error:
                raise RuntimeError(
                    f"runtime package metadata is incomplete: {package.distribution}"
                ) from error
            if stat.S_ISLNK(metadata_mode) or not stat.S_ISREG(metadata_mode):
                raise RuntimeError(
                    f"runtime package metadata is unsafe: {package.distribution}"
                )
        metadata_text = _read_regular_source(
            metadata_file, f"{package.distribution}/METADATA"
        ).decode("utf-8")
        headers: dict[str, str] = {}
        for line in metadata_text.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                headers.setdefault(key.strip().casefold(), value.strip())
        if (
            headers.get("name", "").casefold() != package.distribution.casefold()
            or headers.get("version") != package.version
        ):
            raise RuntimeError(
                f"runtime package identity mismatch: {package.distribution}"
            )
        record_text = _read_regular_source(
            record_file, f"{package.distribution}/RECORD"
        ).decode("utf-8")
        recorded: dict[str, tuple[str, str]] = {}
        try:
            rows = csv.reader(record_text.splitlines())
            for row in rows:
                if len(row) != 3:
                    raise ValueError("invalid RECORD row")
                relative = Path(row[0])
                if relative.parts and relative.parts[0] == package.package:
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("unsafe package RECORD path")
                    if relative.suffix not in {".py", ".typed"}:
                        raise ValueError("non-source package entry")
                    recorded[relative.as_posix()] = (row[1], row[2])
        except (csv.Error, ValueError) as error:
            raise RuntimeError(
                f"runtime package RECORD is invalid: {package.distribution}"
            ) from error
        if not recorded:
            raise RuntimeError(
                f"runtime package has no source RECORD: {package.distribution}"
            )
        package_metadata[package.distribution.casefold()] = (package, recorded)
    return lock_bytes, purelib, package_metadata


def _read_regular_source(
    path: Path, relative_name: str
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise RuntimeError(
            f"runtime source cannot be opened safely: {relative_name}"
        ) from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"runtime source is not a regular file: {relative_name}")
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(_RUNTIME_MAX_BYTES + 1)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev, after.st_ino, after.st_size
        ):
            raise RuntimeError(f"runtime source changed while reading: {relative_name}")
        if len(data) > _RUNTIME_MAX_BYTES:
            raise RuntimeError("runtime source file exceeds the bounded size")
        return data
    finally:
        os.close(fd)


def _package_source_files(
    purelib: Path,
    package: RuntimePackage,
    recorded: dict[str, tuple[str, str]],
) -> dict[str, bytes]:
    source_root = purelib / package.package
    try:
        root_mode = source_root.lstat().st_mode
    except OSError as error:
        raise RuntimeError(
            f"runtime source package is missing: {package.distribution}"
        ) from error
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise RuntimeError(
            f"runtime source package path is unsafe: {package.distribution}"
        )
    if source_root.resolve(strict=True) != source_root:
        raise RuntimeError(
            f"runtime source package resolves unexpectedly: {package.distribution}"
        )

    found: dict[str, bytes] = {}
    total_bytes = 0

    def visit(directory: Path, relative: Path, *, copy_source: bool = True) -> None:
        nonlocal total_bytes
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            child_relative = relative / entry.name
            try:
                mode = path.lstat().st_mode
            except OSError as error:
                raise RuntimeError(
                    "runtime package contains an unreadable entry"
                ) from error
            if stat.S_ISLNK(mode):
                raise RuntimeError("runtime package contains a symlink")
            if stat.S_ISDIR(mode):
                visit(
                    path,
                    child_relative,
                    copy_source=copy_source and entry.name != "__pycache__",
                )
                continue
            if not stat.S_ISREG(mode):
                raise RuntimeError("runtime package contains a special file")
            if entry.name.endswith(".pyc") or "__pycache__" in child_relative.parts:
                continue
            if not copy_source:
                continue
            if path.suffix not in {".py", ".typed"}:
                raise RuntimeError("runtime package contains a non-source file")
            relative_name = (Path(package.package) / child_relative).as_posix()
            if relative_name not in recorded:
                raise RuntimeError(
                    "runtime source entry is not covered by package RECORD"
                )
            hash_record, size_record = recorded[relative_name]
            if not hash_record.startswith("sha256=") or not size_record.isdecimal():
                raise RuntimeError(
                    "runtime package source lacks a bounded SHA-256 RECORD"
                )
            # uv can legitimately hardlink wheel files to its cache. Never
            # publish bytes from the active venv unless the complete package
            # matches its separately frozen source fingerprint below.
            data = _read_regular_source(path, relative_name)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            digest_text = digest.rstrip(b"=").decode("ascii")
            if digest_text != hash_record.partition("=")[2] or len(data) != int(
                size_record
            ):
                raise RuntimeError(
                    "runtime package source does not match package RECORD"
                )
            found[relative_name] = data
            total_bytes += len(data)
            if len(found) > _RUNTIME_MAX_FILES or total_bytes > _RUNTIME_MAX_BYTES:
                raise RuntimeError(
                    "runtime package source exceeds bounded overlay limits"
                )

    visit(source_root, Path())
    if set(found) != set(recorded):
        raise RuntimeError(
            f"runtime package source inventory mismatch: {package.distribution}"
        )
    if _hash_file_map(found) != package.trusted_source_sha256:
        raise RuntimeError(
            f"trusted runtime source fingerprint mismatch: {package.distribution}"
        )
    return found


def _hash_file_map(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _collect_source_runtime(
    runtime: PythonSourceRuntime,
) -> tuple[dict[str, bytes], dict[str, bytes], dict[str, object]]:
    lock_bytes, purelib, package_metadata = _locked_runtime_versions(runtime)
    files: dict[str, bytes] = {}
    package_receipt: dict[str, dict[str, object]] = {}
    for package in runtime.packages:
        metadata = package_metadata.get(package.distribution.casefold())
        if metadata is None or metadata[0] != package:
            raise RuntimeError("trusted runtime package metadata was not resolved")
        source_files = _package_source_files(purelib, package, metadata[1])
        for name, content in source_files.items():
            if name in files:
                raise RuntimeError("duplicate package path in runtime overlay")
            files[name] = content
        package_receipt[package.distribution] = {
            "version": package.version,
            "source_files": len(source_files),
            "source_sha256": _hash_file_map(source_files),
            "fingerprint_provenance": "chief-observed installed source bytes",
        }
    generated: dict[str, bytes] = {}
    for name, content in runtime.generated_files:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not content:
            raise RuntimeError("generated runtime metadata path is invalid")
        if name in generated:
            raise RuntimeError("duplicate generated runtime metadata path")
        generated[name] = content
    overlay_sha256 = _hash_file_map(files)
    if overlay_sha256 != runtime.trusted_overlay_sha256:
        raise RuntimeError("trusted runtime overlay fingerprint mismatch")
    receipt: dict[str, object] = {
        "source": (
            "active Ash virtualenv; uv.lock and installed RECORD checked; "
            "source bytes matched chief-observed fingerprints, not PyPI wheel hashes"
        ),
        "oracle_interpreter": (
            "Linux /usr/bin/python3, Python 3.10 or newer, shared by task and grader"
        ),
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "dependencies": package_receipt,
        "overlay_file_count": len(files),
        "overlay_sha256": overlay_sha256,
        "generated_files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(generated.items())
        },
    }
    return files, generated, receipt


def _ensure_directory(root: Path, relative: Path) -> Path:
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise RuntimeError("runtime directory path is invalid")
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            os.mkdir(current, 0o755)
            mode = current.lstat().st_mode
        except OSError as error:
            raise RuntimeError("runtime directory cannot be inspected") from error
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise RuntimeError("runtime destination contains a symlink or special path")
    return current


def _write_new_file(root: Path, relative_name: str, content: bytes) -> None:
    relative = Path(relative_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("runtime artifact path is invalid")
    parent = _ensure_directory(root, relative.parent)
    destination = parent / relative.name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(destination, flags, 0o644)
    except FileExistsError as error:
        raise RuntimeError("runtime artifact path already exists") from error
    except OSError as error:
        raise RuntimeError("runtime artifact could not be written safely") from error
    with os.fdopen(fd, "wb") as output:
        output.write(content)


def _assert_generated_file_ignored(root: Path, relative_name: str) -> None:
    result = command(
        ["git", "check-ignore", "-q", "--", relative_name],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError("generated pytest version metadata is not Git-ignored")


def materialize_task_runtime(
    root: Path, runtime: PythonSourceRuntime
) -> dict[str, object]:
    """Create only the verified pure-Python runtime artifacts in a fresh checkout."""
    root_mode = root.lstat().st_mode
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise RuntimeError("pinned source checkout is not a plain directory")
    files, generated, receipt = _collect_source_runtime(runtime)
    runtime_root = root / ".benchmark_runtime"
    if runtime_root.exists() or runtime_root.is_symlink():
        raise RuntimeError("runtime overlay path already exists in pinned checkout")
    for name in generated:
        destination = root / name
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("generated runtime metadata path already exists")
    for name, content in sorted(files.items()):
        _write_new_file(root, f".benchmark_runtime/site/{name}", content)
    for name, content in sorted(generated.items()):
        _write_new_file(root, name, content)
        _assert_generated_file_ignored(root, name)
    verify_task_runtime(root, runtime, receipt)
    return receipt


def verify_task_runtime(
    root: Path, runtime: PythonSourceRuntime, expected: dict[str, object]
) -> None:
    """Verify copied runtime bytes exactly before a workspace snapshot."""
    files, generated, receipt = _collect_source_runtime(runtime)
    if receipt != expected:
        raise RuntimeError("runtime source or lock changed after preflight")
    expected_files = {
        f".benchmark_runtime/site/{name}": content for name, content in files.items()
    }
    expected_files.update(generated)
    actual_names: set[str] = set()
    overlay_root = root / ".benchmark_runtime"
    _real_directory(overlay_root)
    site_root = overlay_root / "site"
    _real_directory(site_root)
    for path in overlay_root.rglob("*"):
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            raise RuntimeError(
                "runtime overlay contains an unreadable entry"
            ) from error
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise RuntimeError("runtime overlay contains a symlink or special file")
        if stat.S_ISREG(mode):
            actual_names.add(path.relative_to(root).as_posix())
    if actual_names != set(expected_files) - set(generated):
        raise RuntimeError("runtime overlay contains an unexpected file set")
    for name, content in expected_files.items():
        destination = root / name
        try:
            mode = destination.lstat().st_mode
        except OSError as error:
            raise RuntimeError("required runtime artifact is missing") from error
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise RuntimeError("runtime artifact is not a regular file")
        if _read_regular_source(destination, name) != content:
            raise RuntimeError("runtime artifact bytes differ from the pinned overlay")


def _verify_r17_python_command_parity() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("R17 requires a Linux host with Bubblewrap")
    expected = Path("/usr/bin/python3").resolve()
    for path in ("/usr/bin:/bin", "/usr/local/bin:/usr/bin:/bin"):
        executable = shutil.which("python3", path=path)
        if executable is None or Path(executable).resolve() != expected:
            raise RuntimeError(
                "R17 requires python3 to resolve to /usr/bin/python3 for both runners"
            )


def validate_editable_paths(
    original: dict[str, str],
    editable: frozenset[str],
    preflight_receipt: dict[str, object],
) -> None:
    """Require absent baseline paths to be proven additions in the pinned fix."""
    missing = editable.difference(original)
    raw_added = preflight_receipt.get("reference_added_paths", [])
    if not isinstance(raw_added, list) or any(
        not isinstance(path, str) for path in raw_added
    ):
        raise RuntimeError("preflight reference-addition proof is invalid")
    reference_added = set(raw_added)
    if not missing.issubset(reference_added):
        raise RuntimeError("required source path is absent from both pinned revisions")
    if not reference_added.issubset(editable):
        raise RuntimeError("preflight added a path outside the editable allowlist")


def tool_call_size_metrics(entry: ToolCallRecord) -> dict[str, int | bool]:
    """Report a tool's size/range without copying file contents or paths."""
    metrics: dict[str, int | bool] = {}
    if isinstance(entry.result, str):
        metrics["result_chars"] = len(entry.result)
    if entry.tool_name == "read_file":
        metrics["explicit_max_chars"] = "max_chars" in entry.arguments
        for name in ("max_chars", "start_line", "end_line", "start_column"):
            value = entry.arguments.get(name)
            if type(value) is int and 0 < value <= 131_072:
                metrics[name] = value
    return metrics


def headless_error_category(stdout_path: Path) -> str | None:
    """Classify a headless failure without copying private CLI output to receipts."""
    try:
        # The JSON envelope may contain model content or tool data; never store
        # or echo it. Oversized or malformed output is not a trusted category.
        if stdout_path.stat().st_size > 256 * 1024:
            return None
        events = stdout_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    valid_categories = {category.value for category in ErrorCategory}
    category: str | None = None
    for line in events:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict):
            return None
        if event.get("type") == "error":
            error = event.get("error")
            candidate = error.get("category") if isinstance(error, dict) else None
            if isinstance(candidate, str) and candidate in valid_categories:
                category = candidate
            else:
                return None
    return category


def check_oracles(workspace: Path, task: RepositoryTask) -> dict[str, object]:
    visible = oracle_python(workspace, task.visible_script)
    hidden = oracle_python(workspace, code=task.hidden_source)
    if task.hidden_expected_json is None:
        hidden_correct = hidden.returncode == 0
        hidden_exit = hidden.returncode
    else:
        hidden_correct = _r17_hidden_probe_matches(task, hidden)
        hidden_exit = 0 if hidden_correct else 1
    return {
        "correct": visible.returncode == 0 and hidden_correct,
        "visible_oracle_exit": visible.returncode,
        "hidden_oracle_exit": hidden_exit,
        **(
            {"hidden_probe_exit": hidden.returncode}
            if task.hidden_expected_json is not None
            else {}
        ),
        "oracle_stderr_tail": (visible.stderr + hidden.stderr)[-400:],
    }


def _json_same_typed_value(expected: object, actual: object) -> bool:
    """Compare JSON observations without Python's bool/int numeric coercion."""
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            _json_same_typed_value(left, right)
            for left, right in zip(expected, actual)
        )
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(
            _json_same_typed_value(value, actual[key])
            for key, value in expected.items()
        )
    return expected == actual


def _r17_hidden_probe_matches(
    task: RepositoryTask, completed: subprocess.CompletedProcess[str]
) -> bool:
    if completed.returncode != 0 or task.hidden_expected_json is None:
        return False
    prefix = "R17_PROBE_RESULT:"
    rows = [
        line[len(prefix):]
        for line in completed.stdout.splitlines()
        if line.startswith(prefix)
    ]
    if len(rows) != 1:
        return False
    try:
        expected = json.loads(task.hidden_expected_json)
        actual = json.loads(rows[0])
    except (TypeError, ValueError):
        return False
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False
    required_actual = {"valid_exit", "observations", "unknown_exit", "unknown_output"}
    if set(actual) != required_actual:
        return False
    if (
        type(actual["valid_exit"]) is not int
        or type(actual["unknown_exit"]) is not int
        or not isinstance(actual["observations"], list)
        or not isinstance(actual["unknown_output"], str)
        or len(actual["unknown_output"]) > 4096
    ):
        return False
    unknown_error = expected.get("unknown_error")
    return (
        actual["valid_exit"] == expected.get("valid_exit")
        and _json_same_typed_value(
            expected.get("observations"), actual["observations"]
        )
        and actual["unknown_exit"] == expected.get("unknown_exit")
        and isinstance(unknown_error, str)
        and unknown_error.casefold() in actual["unknown_output"].casefold()
    )


def hidden_oracle_sha256(task: RepositoryTask) -> str:
    """Fingerprint both the candidate-visible probe and its parent-only checker."""
    payload = task.hidden_source.encode("utf-8")
    if task.hidden_expected_json is not None:
        payload += b"\0parent-checker\0" + task.hidden_expected_json.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def hidden_oracle_classification(task: RepositoryTask) -> str:
    if task.hidden_expected_json is not None:
        return "arm-sealed-authored-parent-checker; not an independent holdout"
    return "arm-sealed-authored-inline-oracle"


def _assert_safe_source_tree(root: Path) -> None:
    """Reject pinned trees with links or special files before artifact setup."""
    try:
        root_mode = root.lstat().st_mode
    except OSError as error:
        raise RuntimeError("pinned checkout is unavailable") from error
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise RuntimeError("pinned checkout is not a plain directory")

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
            for entry in entries:
                path = Path(entry.path)
                try:
                    mode = path.lstat().st_mode
                except OSError as error:
                    raise RuntimeError(
                        "pinned checkout has an unreadable path"
                    ) from error
                if stat.S_ISLNK(mode):
                    raise RuntimeError("pinned checkout contains a symlink")
                if stat.S_ISDIR(mode):
                    if not (directory == root and entry.name == ".git"):
                        visit(path)
                elif not stat.S_ISREG(mode):
                    raise RuntimeError("pinned checkout has a special file")
        except OSError as error:
            raise RuntimeError("pinned checkout cannot be inspected safely") from error

    visit(root)


def _attest_r17_system_python_version() -> str:
    """Pin the effective Linux test/grading interpreter for reproducible receipts."""
    version = subprocess.run(
        [
            "/usr/bin/python3", "-B", "-c",
            "import sys; print('.'.join(map(str, sys.version_info[:3])))",
        ],
        cwd=REPO,
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if version.returncode != 0 or not re.fullmatch(
        r"\d+\.\d+\.\d+\n?", version.stdout
    ):
        raise RuntimeError("R17 system Python version could not be attested")
    python_version = version.stdout.strip()
    if tuple(map(int, python_version.split("."))) < (3, 10):
        raise RuntimeError("R17 requires Python 3.10 or newer")
    return python_version


def preflight(task: RepositoryTask, root: Path) -> tuple[Path, dict[str, object]]:
    strict_red_green = (
        task.hidden_expected_json is not None
        or task.require_both_oracles_red_green
    )
    system_python_version: str | None = None
    if strict_red_green:
        _verify_r17_python_command_parity()
        system_python_version = _attest_r17_system_python_version()
    baseline = root / "baseline"
    reference = root / "reference"
    fetch_pinned(baseline, task, task.base_sha)
    fetch_pinned(reference, task, task.reference_fix_sha)
    for tree in (baseline, reference):
        if task.runtime is not None:
            _assert_safe_source_tree(tree)
        visible = tree / task.visible_script
        if visible.exists() or visible.is_symlink():
            raise RuntimeError(
                "upstream checkout collides with visible evaluation test"
            )
    runtime_receipt: dict[str, object] | None = None
    if task.runtime is not None:
        receipts = [
            materialize_task_runtime(tree, task.runtime)
            for tree in (baseline, reference)
        ]
        if receipts[0] != receipts[1]:
            raise RuntimeError("baseline and reference runtime overlays differ")
        runtime_receipt = receipts[0]
    for tree in (baseline, reference):
        visible = tree / task.visible_script
        visible.write_text(task.visible_source, encoding="utf-8")
    base_before = snapshot_files(baseline)
    reference_before = snapshot_files(reference)
    preflight_values = set(base_before.values()) | set(reference_before.values())
    if "SYMLINK" in preflight_values:
        raise RuntimeError("upstream checkout contains a symlink; refusing model trial")
    if {"SPECIAL", "UNREADABLE"}.intersection(preflight_values):
        raise RuntimeError("upstream checkout has special or unreadable files")
    upstream_changed_paths = sorted(
        path
        for path in base_before.keys() | reference_before.keys()
        if base_before.get(path) != reference_before.get(path)
    )
    if (
        task.expected_reference_paths is not None
        and set(upstream_changed_paths) != task.expected_reference_paths
    ):
        raise RuntimeError(
            "pinned reference source paths differ from the task manifest"
        )
    base_score = check_oracles(baseline, task)
    reference_score = check_oracles(reference, task)
    base_visible_red = base_score["visible_oracle_exit"] != 0
    base_hidden_red = base_score["hidden_oracle_exit"] != 0
    reference_both_green = (
        reference_score["visible_oracle_exit"] == 0
        and reference_score["hidden_oracle_exit"] == 0
        and reference_score["correct"] is True
    )
    # Existing R1–R16 tasks only require the original to fail at least one
    # oracle. R17 and tasks that opt in require both original oracles to be red.
    historical_reproduced = (
        base_visible_red and base_hidden_red and reference_both_green
        if strict_red_green
        else not base_score["correct"] and bool(reference_score["correct"])
    )
    if not historical_reproduced:
        raise RuntimeError(
            "upstream historical regression is not independently reproducible"
        )
    ablation_results: list[dict[str, object]] = []
    if task.require_each_editable_ablation_red:
        if not task.editable:
            raise RuntimeError("module ablation requires editable source paths")
        for tree in (baseline, reference):
            _assert_safe_source_tree(tree)
        for editable_path in sorted(task.editable):
            relative = Path(editable_path)
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
            ):
                raise RuntimeError("editable module ablation path is unsafe")
            with tempfile.TemporaryDirectory(
                prefix=".ash-benchmark-ablation-", dir=root
            ) as raw_ablation:
                ablation = Path(raw_ablation) / "source"
                shutil.copytree(
                    reference,
                    ablation,
                    ignore=shutil.ignore_patterns(".git"),
                )
                source_module = baseline / relative
                ablated_module = ablation / relative
                if source_module.is_file():
                    ablated_module.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_module, ablated_module)
                elif ablated_module.is_file():
                    ablated_module.unlink()
                ablation_score = check_oracles(ablation, task)
                visible_exit = ablation_score.get("visible_oracle_exit")
                hidden_exit = ablation_score.get("hidden_oracle_exit")
                ablation_is_red = (
                    ablation_score.get("correct") is False
                    and type(visible_exit) is int
                    and type(hidden_exit) is int
                    and (visible_exit != 0 or hidden_exit != 0)
                )
                if not ablation_is_red:
                    raise RuntimeError(
                        "an editable module ablation did not make the oracle red"
                    )
                ablation_results.append({
                    "path": editable_path,
                    "visible_oracle_exit": visible_exit,
                    "hidden_oracle_exit": hidden_exit,
                })
    base_files = snapshot_files(baseline)
    reference_files = snapshot_files(reference)
    if base_files != base_before or reference_files != reference_before:
        raise RuntimeError("preflight oracle changed a pinned source tree")
    unchanged_editable = {
        path
        for path in task.editable
        if content_identity(base_files.get(path))
        == content_identity(reference_files.get(path))
    }
    if not task.editable.difference(unchanged_editable) or (
        task.require_all_editable_changed and unchanged_editable
    ):
        raise RuntimeError("reference fix does not modify required source files")
    added = sorted(
        path
        for path in task.editable
        if path not in base_files and path in reference_files
    )
    removed = sorted(
        path
        for path in task.editable
        if path in base_files and path not in reference_files
    )
    modified = sorted(
        path
        for path in task.editable
        if path in base_files
        and path in reference_files
        and content_identity(base_files[path]) != content_identity(reference_files[path])
    )
    receipt: dict[str, object] = {
        "base_visible_exit": base_score["visible_oracle_exit"],
        "base_hidden_exit": base_score["hidden_oracle_exit"],
        "reference_visible_exit": reference_score["visible_oracle_exit"],
        "reference_hidden_exit": reference_score["hidden_oracle_exit"],
        "reference_added_paths": added,
        "reference_removed_paths": removed,
        "reference_modified_paths": modified,
    }
    if strict_red_green:
        receipt.update(
            {
                "visible_oracle_sha256": hashlib.sha256(
                    task.visible_source.encode()
                ).hexdigest(),
                "hidden_oracle_sha256": hidden_oracle_sha256(task),
                "hidden_probe_sha256": hashlib.sha256(
                    task.hidden_source.encode()
                ).hexdigest(),
                "hidden_oracle_classification": hidden_oracle_classification(task),
                "system_python_version": system_python_version,
                "system_python_executable": "/usr/bin/python3",
            }
        )
    if task.require_each_editable_ablation_red:
        receipt["editable_ablation_results"] = ablation_results
    if task.expected_reference_paths is not None:
        receipt.update(
            {
                "upstream_changed_paths": upstream_changed_paths,
                "base_editable_sha256": {
                    path: content_identity(base_files.get(path))
                    for path in sorted(task.editable)
                },
                "reference_editable_sha256": {
                    path: content_identity(reference_files.get(path))
                    for path in sorted(task.editable)
                },
            }
        )
    if runtime_receipt is not None:
        receipt.update(
            {
                "base_sha": task.base_sha,
                "reference_fix_sha": task.reference_fix_sha,
                "upstream_pr": task.upstream_pr,
                "license": task.license,
                "runtime_overlay": runtime_receipt,
            }
        )
    return baseline, receipt


def remove_reference_checkout(root: Path) -> None:
    """Discard the historical solution after preflight, before either agent runs."""
    reference = root / "reference"
    if reference.is_symlink() or not reference.is_dir():
        raise RuntimeError("preflight reference checkout missing or unsafe")
    shutil.rmtree(reference)
    if reference.exists() or reference.is_symlink():
        raise RuntimeError("preflight reference checkout was not removed")


def _stop_evaluation_child(child: subprocess.Popen[bytes]) -> int:
    """Reap the isolated Ash subprocess after timeout or interruption."""
    if child.poll() is not None:
        return child.returncode
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass
        try:
            return child.wait(timeout=4)
        except subprocess.TimeoutExpired:
            continue
    return child.wait(timeout=5)


def ash_completion_verified(receipt: dict[str, object]) -> bool:
    """Require a supported terminal sequence for a new, versioned receipt."""
    requested = receipt.get("requested_context_continuations")
    restarts = receipt.get("context_continuations")
    sessions = receipt.get("session_count")
    if (
        type(requested) is not int or requested not in (0, 1, 2)
        or type(restarts) is not int or not 0 <= restarts <= requested
        or type(sessions) is not int or sessions != restarts + 1
    ):
        return False
    terminals = receipt.get("terminal_events")
    reason = receipt.get("terminal_stop_reason")
    prefix = ["turn.error"] * restarts
    if terminals == prefix + ["turn.completed"]:
        return reason == "completed" or (
            reason == "owner_command_checkpoint"
            and receipt.get("requested_report_after_visible_check") is True
        )
    return (
        restarts < requested
        and terminals == prefix + ["turn.error", "turn.finalized.tool_free"]
        and reason == "tool_free_finalization"
    )


def observed_edit_after_passing_test(
    calls: list[dict[str, object]], exact_test: str,
) -> bool | None:
    """Observed tool-call order, not a hash attestation of the tested bytes."""
    after_test: bool | None = None
    for call in calls:
        if call.get("executed") is not True or call.get("success") is not True:
            continue
        if (call.get("tool") == "run_command" and call.get("command") == exact_test
                and type(call.get("exit_code")) is int and call["exit_code"] == 0):
            after_test = False
        elif (after_test is not None and isinstance(call.get("tool"), str)
              and call["tool"] in EDIT_TOOL_NAMES
              and not (call["tool"] == "apply_patch" and call.get("dry_run") is True)):
            after_test = True
    return after_test


def collect_coding_session_receipts(
    store: SessionStore, workspace: Path,
) -> dict[str, object]:
    """Aggregate each fresh task's parent and context-restart child safely."""
    sessions = sorted(
        store.list_sessions(project_path=str(workspace)),
        key=lambda item: (item.created_at, item.session_id),
    )
    calls: list[dict[str, object]] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    events = []
    terminals: list[str] = []
    audit_ok = bool(sessions)
    lineage_ok = bool(sessions)
    previous_session_id: str | None = None
    for summary in sessions:
        session = store.load_session(summary.session_id)
        if previous_session_id is None:
            lineage_ok = lineage_ok and session.parent_session_id is None
        else:
            lineage_ok = (
                lineage_ok and session.parent_session_id == previous_session_id
                and session.fork_message_count == 0
            )
        previous_session_id = session.session_id
        calls.extend({
            "tool": entry.tool_name,
            "success": entry.success is True,
            "executed": entry.executed,
            "approved": entry.approved,
            "error": (entry.error or "")[:180],
            "exit_code": entry.exit_code,
            "command": entry.arguments.get("command_line", "")
            if entry.tool_name == "run_command"
            else "",
            "size_metrics": tool_call_size_metrics(entry),
            # The actual patch tool accepts a boolean dry-run option. Preserve
            # just its content-free classification, not patch text or arguments.
            **({"dry_run": entry.arguments.get("dry_run") is True}
               if entry.tool_name == "apply_patch" else {}),
        } for entry in session.tool_calls)
        audit_ok = audit_ok and not store.verify_audit_log(session.session_id)
        used = store.get_session_usage(session.session_id)
        usage["prompt_tokens"] += used.prompt_tokens
        usage["completion_tokens"] += used.completion_tokens
        events.extend(store.list_runtime_events(session.session_id, limit=10_000))
    events.sort(key=lambda entry: entry.sequence)
    terminal_rows = [
        row.event for row in events
        if row.event.get("type") in {
            "turn.completed", "turn.cancelled", "turn.error", "turn.finalized.tool_free",
        }
    ]
    terminals = [str(event["type"]) for event in terminal_rows]
    terminal_stop_reason = None
    if terminal_rows:
        last = terminal_rows[-1]
        if last["type"] == "turn.completed" and isinstance(last.get("stop_reason"), str):
            terminal_stop_reason = last["stop_reason"]
        elif (last["type"] == "turn.finalized.tool_free"
              and type(last.get("tool_calls_executed")) is int
              and last["tool_calls_executed"] == 0):
            terminal_stop_reason = "tool_free_finalization"
    return {
        "calls": calls,
        "audit_ok": audit_ok,
        "lineage_ok": lineage_ok,
        "session_count": len(sessions),
        "context_continuations": max(0, len(sessions) - 1),
        "usage": usage,
        "events": summarize_operational_events(events),
        "terminals": terminals,
        "terminal_stop_reason": terminal_stop_reason,
    }


def run_task(
    task_name: str,
    *,
    model: str,
    account: ChatGPTAccount,
    evidence: Path,
    timeout: int,
    context_tokens: int,
    turn_token_budget: int,
    reasoning_effort: str | None = None,
    fair_prompt: bool = False,
    context_continuations: int = 0,
    turn_time_budget: int | None = None,
    report_after_visible_check: bool = False,
) -> dict[str, object]:
    task = TASKS[task_name]
    user_prompt = shared_user_prompt(task) if fair_prompt else task.prompt
    started = time.monotonic()
    with (
        tempfile.TemporaryDirectory(prefix="ash-repository-eval-") as scratch_str,
        tempfile.TemporaryDirectory(
            prefix="ash-chatgpt-benchmark-", dir=_chatgpt_tmpfs_root()
        ) as home_str,
    ):
        scratch = Path(scratch_str)
        home = Path(home_str)
        baseline, preflight_receipt = preflight(task, scratch)
        remove_reference_checkout(scratch)
        workspace = scratch / "workspace"
        # Preserve link types instead of following upstream links into any
        # filesystem paths outside the pinned source checkout.
        shutil.copytree(baseline, workspace, symlinks=True)
        if task.runtime is not None:
            runtime_receipt = preflight_receipt.get("runtime_overlay")
            if not isinstance(runtime_receipt, dict):
                raise RuntimeError("preflight omitted the required runtime receipt")
            verify_task_runtime(workspace, task.runtime, runtime_receipt)
        original = snapshot_files(workspace)
        validate_editable_paths(original, task.editable, preflight_receipt)
        _stage_chatgpt_access_only(home, account, timeout=timeout)
        (home / ".ash" / "ash.toml").write_text('sandbox_backend = "native"\n')
        db = scratch / "db"
        db.mkdir()
        ephemeral_tmp = scratch / "tmp"
        ephemeral_tmp.mkdir()
        env = {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "USER": "ash-repository-eval",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(ephemeral_tmp),
            "PYTHONPATH": str(REPO / "src"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "ASH_MODEL": f"openai/{model}",
            "ASH_OPENAI_AUTH_MODE": "chatgpt",
            "ASH_MAX_CONTEXT_TOKENS": str(context_tokens),
            "ASH_MAX_TURN_TOTAL_TOKENS": str(turn_token_budget),
            "ASH_MAX_TURN_ITERATIONS": str(CHATGPT_TURN_ITERATIONS),
            "NO_PROXY": "127.0.0.1,localhost",
        }
        trust = command(
            [str(PYTHON), "-m", "ash", "trust", "add", str(workspace)],
            cwd=workspace,
            env=env,
            timeout=20,
        )
        if trust.returncode:
            raise RuntimeError("isolated workspace trust setup failed")
        grant = command(
            [
                str(PYTHON),
                "-m",
                "ash",
                "permissions",
                "allow",
                "run_command",
                "--exact",
                "command_line=" + json.dumps(task.command_grant),
            ],
            cwd=workspace,
            env=env,
            timeout=20,
        )
        if grant.returncode:
            raise RuntimeError("isolated test command grant failed")

        argv = [
            str(PYTHON),
            "-m",
            "ash",
            "--db-directory",
            str(db),
            "--mode",
            "auto_edit",
            "-p",
            user_prompt,
            "--output-format",
            "json",
        ]
        if reasoning_effort is not None:
            argv.extend(["--reasoning-effort", reasoning_effort])
        if context_continuations:
            argv.extend(["--context-continuations", str(context_continuations)])
        if turn_time_budget is not None:
            argv.extend(["--turn-time-budget", str(turn_time_budget)])
        if report_after_visible_check:
            argv.extend(["--report-after-command", task.command_grant])
        cli_started = time.monotonic()
        with (
            (evidence / "ash.stdout").open("wb") as stdout,
            (evidence / "ash.stderr").open("wb") as stderr,
        ):
            child = subprocess.Popen(
                argv,
                cwd=workspace,
                env=env,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            timed_out = False
            try:
                exit_code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = _stop_evaluation_child(child)
            except BaseException:
                _stop_evaluation_child(child)
                raise
        cli_seconds = round(time.monotonic() - cli_started, 2)

        updated = snapshot_files(workspace)
        protected, changed = check_edits(
            original,
            updated,
            task.editable,
            require_all_editable_changed=task.require_all_editable_changed,
        )
        oracle = check_oracles(workspace, task)
        store = SessionStore(db / "sessions.db")
        aggregated = collect_coding_session_receipts(store, workspace)
        calls = aggregated["calls"]
        if not isinstance(calls, list) or any(
            not isinstance(call, dict) for call in calls
        ):
            raise RuntimeError("Coding session tool-call receipts are malformed")
        audit_ok = aggregated["audit_ok"] and aggregated["lineage_ok"]
        event_stats = aggregated["events"]
        usage = aggregated["usage"]
        terminals = aggregated["terminals"]
        visible_test_calls = [
            call for call in calls
            if call["tool"] == "run_command"
            and call["command"] == task.command_grant
        ]
        visible_test_command_count = sum(
            call["executed"] is True for call in visible_test_calls
        )
        visible_test_command_executed = visible_test_command_count > 0
        visible_test_command_passed = any(
            call["executed"] is True
            and call["success"] is True
            and type(call["exit_code"]) is int
            and call["exit_code"] == 0
            for call in visible_test_calls
        )
        required = any(
            call["tool"] in EDIT_TOOL_NAMES and call["success"] and call["executed"]
            for call in calls
        ) and any(
            call["tool"] == "run_command"
            and call["success"]
            and call["executed"]
            and type(call["exit_code"]) is int
            and call["exit_code"] == 0
            and call["command"] == task.command_grant
            for call in calls
        )
        if task.require_single_visible_test:
            required = required and visible_test_command_count == 1
        result: dict[str, object] = {
            "task": task_name,
            "source": task.source,
            "upstream_pr": task.upstream_pr,
            "license": task.license,
            "base_sha": task.base_sha,
            "reference_fix_sha": task.reference_fix_sha,
            "diagnostic_retake_of": task.diagnostic_retake_of,
            "evidence_classification": task_evidence_classification(task),
            "task_prompt_sha256": hashlib.sha256(task.prompt.encode()).hexdigest(),
            "user_prompt_sha256": hashlib.sha256(user_prompt.encode()).hexdigest(),
            "visible_oracle_sha256": hashlib.sha256(
                task.visible_source.encode()
            ).hexdigest(),
            "hidden_oracle_sha256": hidden_oracle_sha256(task),
            "hidden_probe_sha256": hashlib.sha256(
                task.hidden_source.encode()
            ).hexdigest(),
            "hidden_oracle_classification": hidden_oracle_classification(task),
            "fair_prompt": fair_prompt,
            "preflight": preflight_receipt,
            "ash_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
            ).strip(),
            "provider_model": f"openai/{model}",
            "context_tokens": context_tokens,
            "turn_token_budget": turn_token_budget,
            "iteration_limit": CHATGPT_TURN_ITERATIONS,
            "reasoning_effort": reasoning_effort,
            "requested_context_continuations": context_continuations,
            "requested_turn_time_budget": turn_time_budget,
            "requested_report_after_visible_check": report_after_visible_check,
            "context_continuations": aggregated["context_continuations"],
            "session_count": aggregated["session_count"],
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "cli_seconds": cli_seconds,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "terminal_error_category": headless_error_category(evidence / "ash.stdout"),
            "oracle": oracle,
            "protected_files_unchanged": protected,
            "required_sources_changed": changed,
            "required_tools_succeeded": required,
            "visible_test_command_executed": visible_test_command_executed,
            "visible_test_command_passed": visible_test_command_passed,
            "visible_test_command_count": visible_test_command_count,
            "audit_ok": audit_ok,
            "terminal_events": terminals,
            "terminal_stop_reason": aggregated["terminal_stop_reason"],
            "completion_evidence_version": 1,
            "observed_edit_after_passing_test": observed_edit_after_passing_test(
                calls, task.command_grant,
            ),
            "usage": usage,
            "tool_calls": calls,
            "performance": event_stats,
            "changed_paths": sorted(
                path
                for path in set(original) | set(updated)
                if original.get(path) != updated.get(path)
            ),
            "passed": bool(
                exit_code == 0
                and not timed_out
                and oracle["correct"]
                and protected
                and changed
                and required
                and audit_ok
            ),
        }
        result["verification_order_version"] = VERIFICATION_ORDER_VERSION
        result["verification_freshness"] = verification_freshness(
            result["observed_edit_after_passing_test"],
        )
        result["completion_verified"] = ash_completion_verified(result)
        result["passed"] = bool(
            result["passed"] and result["completion_verified"]
            and result["verification_freshness"] != "observed_stale"
        )
        (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    result["scratch_removed"] = True
    result["credential_home_removed"] = True
    (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=tuple(TASKS), required=True)
    parser.add_argument("--chatgpt-model", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=210)
    parser.add_argument("--context-tokens", type=int, default=32768)
    parser.add_argument(
        "--turn-token-budget",
        type=int,
        default=0,
        help=(
            "Cumulative prompt and completion tokens per Ash turn; "
            "0 uses Ash's production default (no cumulative cap). "
            "Per-request context, iteration and wall-time bounds still apply."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        default=None,
        help="Explicit model-validated reasoning effort for a controlled trial",
    )
    parser.add_argument(
        "--fair-prompt",
        action="store_true",
        help="Use the exact model-neutral user task shared with plain Codex",
    )
    parser.add_argument(
        "--context-continuations", type=int, default=0, choices=(0, 1, 2),
        help="Opt in to bounded safe Ash reasoning-window restart on overflow",
    )
    parser.add_argument(
        "--turn-time-budget", type=int, default=None,
        help="Optional Ash one-shot wall-time budget (30..3600s), independent of external timeout",
    )
    parser.add_argument(
        "--report-after-visible-check", action="store_true",
        help="Explicit operator checkpoint: finish with a bounded report when the already-granted visible command succeeds after edits",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", args.chatgpt_model):
        parser.error("invalid exact ChatGPT model ID")
    if not 30 <= args.timeout <= 300:
        parser.error("timeout must be between 30 and 300 seconds")
    # The production default is 128K. The evaluation runner must be able to
    # exercise it without lifting provider, approval, or resource boundaries.
    if not 8192 <= args.context_tokens <= 131072:
        parser.error("context budget must be between 8192 and 131072")
    if args.turn_token_budget != 0 and not 2000 <= args.turn_token_budget <= 250000:
        parser.error("turn budget must be 0 or between 2000 and 250000")
    if args.turn_time_budget is not None and not 30 <= args.turn_time_budget <= args.timeout:
        parser.error("turn-time-budget must be between 30 seconds and the external timeout")
    if not PYTHON.is_file() or not shutil.which("bwrap") or not shutil.which("git"):
        parser.error("repository benchmark requires Ash venv, Git, and Bubblewrap")
    try:
        _chatgpt_tmpfs_root()
        account = ChatGPTCredentialStore().active()
        if account is None or not account.access_usable(
            now=time.time() + args.timeout + 30
        ):
            raise ValueError("active ChatGPT access must cover the trial")
        catalog = {slug for slug, _ in asyncio.run(list_chatgpt_models())}
        if args.chatgpt_model not in catalog:
            raise ValueError("selected model not available in active ChatGPT plan")
    except ValueError as exc:
        parser.error(str(exc))
    except Exception as exc:
        parser.error("ChatGPT authorization preflight failed: " + type(exc).__name__)
    if args.evidence_dir.exists():
        parser.error("evidence directory already exists; never overwrite a trial")
    args.evidence_dir.mkdir(parents=True, mode=0o700)
    result = run_task(
        args.task,
        model=args.chatgpt_model,
        account=account,
        evidence=args.evidence_dir,
        timeout=args.timeout,
        context_tokens=args.context_tokens,
        turn_token_budget=args.turn_token_budget,
        reasoning_effort=args.reasoning_effort,
        fair_prompt=args.fair_prompt,
        context_continuations=args.context_continuations,
        turn_time_budget=args.turn_time_budget,
        report_after_visible_check=args.report_after_visible_check,
    )
    print(
        json.dumps(
            {
                "task": result["task"],
                "passed": result["passed"],
                "oracle": result["oracle"],
                "elapsed_seconds": result["elapsed_seconds"],
            }
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
