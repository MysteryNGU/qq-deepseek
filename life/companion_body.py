"""Await the existing desktop body's real tool receipts, without another runtime."""
import asyncio
from concurrent.futures import Future
import io
import json
import math
from pathlib import Path
import time
import uuid

from jsonschema import Draft7Validator


def _object(properties, required=()):
    return dict(type='object', properties=properties, required=list(required), additionalProperties=False)


STRING = dict(type='string', minLength=1)
BOOLEAN = dict(type='boolean')
SCHEMAS = {
    'pet_move_to': _object(dict(to=dict(anyOf=[dict(type='number', minimum=0, maximum=1), STRING]),
        run=BOOLEAN, params=dict(type='array', items=dict(type='number'), maxItems=2)), ['to']),
    'pet_act': _object(dict(actions=dict(type='array', items=STRING, minItems=1, maxItems=6)), ['actions']),
    'pet_quiet': _object(dict(minutes=dict(type='number', minimum=0, maximum=1440), sound=BOOLEAN,
        roam=dict(enum=['off', 'calm'])), ['minutes']),
    'pet_see_self': _object({}),
    'pet_set': _object(dict(figure=STRING, scheme=dict(type='string'), roam=dict(enum=['free', 'calm', 'off']),
        snoreSeconds=dict(type='integer', minimum=0, maximum=3600), sound=BOOLEAN,
        scale=dict(type='number', minimum=.5, maximum=10),
        bubbleSize=dict(type='number', minimum=.8, maximum=1.6),
        fontSize=dict(type='number', minimum=.8, maximum=1.6), theme=dict(enum=['dark', 'light']),
        hoverButtons=dict(type='array', maxItems=6, items=dict(enum=[
            'chat', 'voice', 'roam', 'theme', 'sound', 'size', 'dress', 'hide', 'worlds'])),
        user=dict(type='string'))),
}


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Body arguments require finite JSON numbers')
    return number


class BodyExecutor:
    def __init__(self, control=None, *, home=None):
        self.control = control
        self.home = Path(home).resolve() if home is not None else None
        self._pending = set()
        self._closed = False

    def available_kinds(self):
        return ['body'] if callable(self.control) and not self._closed else []

    async def run(self, action):
        if not self.available_kinds():
            raise RuntimeError('Desktop body is unavailable or closed')
        op = action.get('op')
        if action.get('kind') != 'body' or op not in SCHEMAS:
            raise ValueError('Unknown body operation')
        try:
            args = json.loads(action.get('arguments') or '{}', parse_float=_finite, parse_constant=_finite)
        except (ValueError, TypeError):
            raise ValueError('Body arguments must be a JSON object with finite numbers') from None
        error = next(Draft7Validator(SCHEMAS[op]).iter_errors(args), None)
        if error:
            raise ValueError('Body '+op+' arguments: '+error.message)
        started = time.monotonic()
        future = None
        try:
            future = self.control(op, args)
            if not isinstance(future, Future):
                raise RuntimeError('Desktop body control did not return a Future')
            self._pending.add(future)
            receipt = await asyncio.wrap_future(future)
            if not isinstance(receipt, dict):
                raise RuntimeError('Desktop body returned an invalid receipt')
            receipt = dict(receipt)
            blobs = receipt.pop('blobs', [])
            status = receipt.get('status')
            if receipt.get('failed'):
                status = 'failed'
            elif receipt.get('cancelled') or receipt.get('interrupted'):
                status = 'interrupted'
            elif status is None and op != 'pet_move_to':
                status = 'completed'
            result = dict(status=status if status in {'completed', 'failed', 'interrupted'} else 'failed',
                          output=str(receipt.get('text', ''))[:12000], receipt=receipt)
            if status not in {'completed', 'failed', 'interrupted'}:
                result['code'] = 'body_receipt_unverified'
            elif receipt.get('code'):
                result['code'] = receipt['code']
            if op == 'pet_act' and result['status'] == 'completed':
                result['completion'] = 'accepted'
            if op == 'pet_see_self' and result['status'] == 'completed' and not blobs:
                raise RuntimeError('Body portrait receipt has no actual image')
            if blobs and result['status'] == 'completed':
                result['image_path'] = self._portrait(blobs)
        except asyncio.CancelledError:
            if future is not None:
                future.cancel()  # CompanionShell forwards this as control_cancel/AbortSignal.
            raise
        except Exception as error:
            result = dict(status='failed', code='body_error', output=str(error)[:12000])
        finally:
            if future is not None:
                self._pending.discard(future)
        result['seconds'] = round(time.monotonic()-started, 3)
        if self.home is not None:
            folder = self.home/'data/companion/artifacts'; folder.mkdir(parents=True, exist_ok=True)
            path = folder/(uuid.uuid4().hex+'.body.json')
            result['output_path'] = str(path)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result

    def _portrait(self, blobs):
        from PIL import Image
        if self.home is None:
            raise ValueError('A character home is required to save the actual body portrait')
        if not isinstance(blobs, list) or len(blobs) != 1 or not isinstance(blobs[0], dict):
            raise ValueError('Body portrait must contain one actual image')
        blob = blobs[0]; raw = blob.get('bytes')
        if isinstance(raw, dict) and raw.get('type') == 'Buffer':
            raw = raw.get('data')
        if isinstance(raw, list) and len(raw) <= 32*1024*1024 and all(type(v) is int and 0 <= v <= 255 for v in raw):
            raw = bytes(raw)
        if not isinstance(raw, bytes) or len(raw) > 32*1024*1024:
            raise ValueError('Body portrait has invalid or oversized image bytes')
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != 'PNG' or blob.get('mime') != 'image/png':
                raise ValueError('Body portrait must be a real PNG')
            image.verify()
        folder = self.home/'data/companion/artifacts'; folder.mkdir(parents=True, exist_ok=True)
        path = folder/(uuid.uuid4().hex+'.body.png'); path.write_bytes(raw)
        return str(path)

    async def close(self):
        self._closed = True
        pending = list(self._pending)
        for future in pending:
            future.cancel()
        await asyncio.gather(*(asyncio.wrap_future(future) for future in pending), return_exceptions=True)
