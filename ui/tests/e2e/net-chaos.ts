/**
 * Controllable TCP proxy for E2E network failures (test harness only, loopback only).
 *
 * Chromium's offline emulation does not tear down an already established localhost stream, so the SSE
 * reconnect tests need real network failures. The harness runs two instances:
 *   "ui"  browser → proxy → vite preview   (drop/block = the browser loses its connections)
 *   "api" vite preview → proxy → uvicorn   (drop = half-open stream: browser side stays open but silent)
 * Bytes are forwarded unchanged; a tiny control endpoint offers
 *   POST /drop     destroy every open connection (client and upstream side)
 *   POST /block    refuse new connections (accepted sockets are closed immediately) and drop open ones
 *   POST /unblock  forward again
 *   GET  /stats    { open, accepted, blocked, dropped } (dropped = sockets destroyed by this call)
 */
import http from "node:http";
import net from "node:net";

export interface ChaosProxy {
  readonly port: number;
  readonly controlUrl: string;
  close(): Promise<void>;
}

export async function startChaosProxy(targetPort: number, listenPort: number, controlPort: number): Promise<ChaosProxy> {
  const open = new Set<net.Socket>();
  let blocked = false;
  let accepted = 0;

  const dropAll = (): number => {
    const n = open.size;
    for (const s of open) s.destroy();
    open.clear();
    return n;
  };

  const proxy = net.createServer((client) => {
    accepted += 1;
    if (blocked) {
      client.destroy();
      return;
    }
    const upstream = net.connect(targetPort, "127.0.0.1");
    open.add(client);
    open.add(upstream);
    const cleanup = () => {
      open.delete(client);
      open.delete(upstream);
      client.destroy();
      upstream.destroy();
    };
    client.on("error", cleanup);
    upstream.on("error", cleanup);
    client.on("close", cleanup);
    upstream.on("close", cleanup);
    client.pipe(upstream);
    upstream.pipe(client);
  });

  const control = http.createServer((req, res) => {
    const url = req.url ?? "/";
    let dropped = 0;
    if (req.method === "POST" && url === "/drop") dropped = dropAll();
    else if (req.method === "POST" && url === "/block") {
      blocked = true;
      dropped = dropAll();
    } else if (req.method === "POST" && url === "/unblock") blocked = false;
    else if (!(req.method === "GET" && url === "/stats")) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ open: open.size, accepted, blocked, dropped }));
  });

  const listen = (srv: net.Server, port: number) =>
    new Promise<void>((resolve, reject) => {
      srv.once("error", reject);
      srv.listen(port, "127.0.0.1", () => {
        srv.off("error", reject);
        resolve();
      });
    });
  await listen(proxy, listenPort);
  await listen(control, controlPort);

  return {
    port: listenPort,
    controlUrl: `http://127.0.0.1:${String(controlPort)}`,
    close: async () => {
      dropAll();
      await Promise.all([
        new Promise<void>((r) => proxy.close(() => r())),
        new Promise<void>((r) => control.close(() => r())),
      ]);
    },
  };
}
