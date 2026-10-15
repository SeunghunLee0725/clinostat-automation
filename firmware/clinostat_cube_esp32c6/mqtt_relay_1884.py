"""0.0.0.0:1884 -> 127.0.0.1:1883 TCP 중계. 상위 방화벽이 1883 을 drop 해서 보드가 1884 로 들어온다."""
import asyncio

async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data); await w.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        w.close()

async def handle(cr, cw):
    try:
        ur, uw = await asyncio.open_connection("127.0.0.1", 1883)
    except OSError:
        cw.close(); return
    await asyncio.gather(pipe(cr, uw), pipe(ur, cw))

async def main():
    srv = await asyncio.start_server(handle, "0.0.0.0", 1884)
    async with srv: await srv.serve_forever()

asyncio.run(main())
