"""Camb AI: REST for the recorded-clip chain, a WebSocket for live.

``provider``  the three-call chain: transcribe, translate, speak
``realtime``  the live session over ``wss://realtime.camb.ai``
``mapper``    their create/poll/fetch JSON <-> our types, pure
``protocol``  the realtime wire format, pure
``tts``       the one endpoint that returns bytes, not JSON
"""
