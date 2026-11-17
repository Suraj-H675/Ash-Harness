"""Independently grade a historical bug repair in a pinned upstream repository.

Opt-in, operator-run evaluation. Never run against a developer's live checkout.
The visible test is in the disposable source tree; the hidden oracle is not.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

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
    content_identity,
    snapshot_workspace_files as snapshot_files,
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


TASKS = {
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
}


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
    original: dict[str, str], updated: dict[str, str], editable: frozenset[str]
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
    changed = bool(editable) and all(
        content_identity(original.get(path)) != content_identity(updated.get(path))
        for path in editable
    )
    return protected, changed


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
    return {
        "correct": visible.returncode == 0 and hidden.returncode == 0,
        "visible_oracle_exit": visible.returncode,
        "hidden_oracle_exit": hidden.returncode,
        "oracle_stderr_tail": (visible.stderr + hidden.stderr)[-400:],
    }


def preflight(task: RepositoryTask, root: Path) -> tuple[Path, dict[str, object]]:
    baseline = root / "baseline"
    reference = root / "reference"
    fetch_pinned(baseline, task, task.base_sha)
    fetch_pinned(reference, task, task.reference_fix_sha)
    preflight_values = {
        value
        for tree in (baseline, reference)
        for value in snapshot_files(tree).values()
    }
    if "SYMLINK" in preflight_values:
        raise RuntimeError("upstream checkout contains a symlink; refusing model trial")
    if {"SPECIAL", "UNREADABLE"}.intersection(preflight_values):
        raise RuntimeError("upstream checkout has special or unreadable files")
    for tree in (baseline, reference):
        visible = tree / task.visible_script
        if visible.exists() or visible.is_symlink():
            raise RuntimeError(
                "upstream checkout collides with visible evaluation test"
            )
        visible.write_text(task.visible_source, encoding="utf-8")
    base_score = check_oracles(baseline, task)
    reference_score = check_oracles(reference, task)
    if base_score["correct"] or not reference_score["correct"]:
        raise RuntimeError(
            "upstream historical regression is not independently reproducible"
        )
    base_files = snapshot_files(baseline)
    reference_files = snapshot_files(reference)
    if any(
        content_identity(base_files.get(path))
        == content_identity(reference_files.get(path))
        for path in task.editable
    ):
        raise RuntimeError("reference fix does not modify all required source files")
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
    return baseline, {
        "base_visible_exit": base_score["visible_oracle_exit"],
        "base_hidden_exit": base_score["hidden_oracle_exit"],
        "reference_visible_exit": reference_score["visible_oracle_exit"],
        "reference_hidden_exit": reference_score["hidden_oracle_exit"],
        "reference_added_paths": added,
        "reference_removed_paths": removed,
        "reference_modified_paths": modified,
    }


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
            "command": entry.arguments.get("command_line", "")
            if entry.tool_name == "run_command"
            else "",
            "size_metrics": tool_call_size_metrics(entry),
        } for entry in session.tool_calls)
        audit_ok = audit_ok and not store.verify_audit_log(session.session_id)
        used = store.get_session_usage(session.session_id)
        usage["prompt_tokens"] += used.prompt_tokens
        usage["completion_tokens"] += used.completion_tokens
        events.extend(store.list_runtime_events(session.session_id, limit=10_000))
    events.sort(key=lambda entry: entry.sequence)
    terminals = [
        str(row.event.get("type"))
        for row in events
        if row.event.get("type") in {"turn.completed", "turn.cancelled", "turn.error"}
    ]
    return {
        "calls": calls,
        "audit_ok": audit_ok,
        "lineage_ok": lineage_ok,
        "session_count": len(sessions),
        "context_continuations": max(0, len(sessions) - 1),
        "usage": usage,
        "events": summarize_operational_events(events),
        "terminals": terminals,
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
        protected, changed = check_edits(original, updated, task.editable)
        oracle = check_oracles(workspace, task)
        store = SessionStore(db / "sessions.db")
        aggregated = collect_coding_session_receipts(store, workspace)
        calls = aggregated["calls"]
        audit_ok = aggregated["audit_ok"] and aggregated["lineage_ok"]
        event_stats = aggregated["events"]
        usage = aggregated["usage"]
        terminals = aggregated["terminals"]
        required = any(
            call["tool"] in EDIT_TOOL_NAMES and call["success"] and call["executed"]
            for call in calls
        ) and any(
            call["tool"] == "run_command"
            and call["success"]
            and call["executed"]
            and call["command"] == task.command_grant
            for call in calls
        )
        result: dict[str, object] = {
            "task": task_name,
            "source": task.source,
            "base_sha": task.base_sha,
            "reference_fix_sha": task.reference_fix_sha,
            "task_prompt_sha256": hashlib.sha256(task.prompt.encode()).hexdigest(),
            "user_prompt_sha256": hashlib.sha256(user_prompt.encode()).hexdigest(),
            "visible_oracle_sha256": hashlib.sha256(
                task.visible_source.encode()
            ).hexdigest(),
            "hidden_oracle_sha256": hashlib.sha256(
                task.hidden_source.encode()
            ).hexdigest(),
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
            "audit_ok": audit_ok,
            "terminal_events": terminals,
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
