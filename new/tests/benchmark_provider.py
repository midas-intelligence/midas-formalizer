"""Alternating local provider buffering/protocol comparison; no network or Lean."""
import asyncio
from dataclasses import replace
from pathlib import Path
from statistics import median
import sys
from timeit import timeit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from midas_formalizer.model_adapters import Collector as PreviousCollector, ResponsesAdapter
from midas_formalizer.model_protocol import ModelLimits
from midas.provider.transport import Collector
from midas.provider.responses import ResponsesTransport
from tests.test_provider import LIMITS


def compare(label, old, new, count):
    samples = ([], [])
    for sample in range(7):
        for side in (sample % 2, 1 - sample % 2):
            samples[side].append(timeit((old, new)[side], number=count)/count)
    before, after = map(median, samples)
    print(f'{label}: v2={before*1e6:.1f} us new={after*1e6:.1f} us ratio={after/before:.3f}')


def main():
    for length, chunk in ((131072,128),(1048576,1024)):
        old_limits = ModelLimits(output_bytes=length,frame_bytes=max(length,1048576))
        limits = replace(LIMITS,output_bytes=length,frame_bytes=max(length,1048576))
        delta = 'x'*chunk
        def previous():
            c = PreviousCollector(old_limits)
            for _ in range(length//chunk):
                c.text(delta,append=True)
            return c.value['raw'].encode()
        def current():
            c = Collector(limits)
            for _ in range(length//chunk):
                c.text(delta,append=True)
            return bytes(c.raw)
        assert previous() == current()
        compare(f'text_{length}_bytes',previous,current,5)

    body = (b'data: {"type":"response.output_text.delta","delta":"' + b'x'*128 + b'"}\n\n')*1024
    body += b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    class Stream:
        async def aiter_bytes(self):
            for start in range(0,len(body),8192):
                yield body[start:start+8192]
    def previous_stream():
        c = PreviousCollector(ModelLimits())
        asyncio.run(ResponsesAdapter._stream(Stream(),c))
        return c.value['raw'].encode()
    def current_stream():
        c = Collector(LIMITS)
        asyncio.run(ResponsesTransport.stream(Stream(),c))
        return bytes(c.raw)
    assert previous_stream() == current_stream()
    compare('SSE_1024_deltas',previous_stream,current_stream,5)


if __name__ == '__main__':
    main()
