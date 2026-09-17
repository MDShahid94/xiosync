#!/usr/bin/env node
// ─── XIO-BROWSER CLI Entry ─────────────────────────────────────────────────
// Usage:
//   --stdio                  MCP stdio transport (AI client spawns process)
//   --http <PORT>            MCP HTTP/SSE transport (remote access)
//   --drive <PATH>           Root path of mounted Shared Drive (default: /content/drive/Shareddrives/XIO_MESH)
//   --default-exit <IP>      Default Tailscale exit node IP (default: 100.86.149.127)
//   --db <PATH>              Override SQLite DB path (default: <drive>/xio-browser.db)
//   --device-specs <JSON>    JSON object with real hardware specs of the exit node device.
//                            Overrides hardcoded fingerprint fallbacks. Example:
//                            '{"cores":10,"ram":16,"webgl_renderer":"Apple M4","macos_version":"15.2.0","width":2560,"height":1440}'

import { parseArgs } from 'node:util';
import path from 'node:path';
import { createServer } from '../src/mcp-server.mjs';
import { createLogger } from '../src/utils/logger.mjs';

const log = createLogger('cli');

const { values: args } = parseArgs({
  options: {
    stdio:            { type: 'boolean', default: false },
    http:             { type: 'string' },
    drive:            { type: 'string', default: '/content/drive/Shareddrives/XIO_MESH' },
    'default-exit':   { type: 'string', default: '100.86.149.127' },
    db:               { type: 'string' },
    'device-specs':   { type: 'string' },  // JSON string with real exit node hardware specs
    'ts-proxy':       { type: 'string', default: null }, // B13: SOCKS5 proxy for userspace Tailscale
  },
  strict: false,
});

if (!args.stdio && !args.http) {
  // Default to stdio when no flag given
  args.stdio = true;
}

const config = {
  driveRoot:    args.drive,
  defaultExit:  args['default-exit'],
  dbPath:       args.db ?? path.join('/content', 'xio-browser.db'),
  tsProxy:      args['ts-proxy'] ?? null,   // B13: e.g. "socks5://127.0.0.1:1055"
  deviceSpecs:  (() => {
    const raw = args['device-specs'];
    if (!raw) return null;
    try { return JSON.parse(raw); }
    catch (e) { log.warn(`Invalid --device-specs JSON: ${e.message}`); return null; }
  })(),
};

log.info(`XIO-BROWSER starting`);
log.info(`Drive root   : ${config.driveRoot}`);
log.info(`Default exit : ${config.defaultExit}`);
log.info(`DB path      : ${config.dbPath}`);
if (config.tsProxy)     log.info(`TS proxy     : ${config.tsProxy}`);
if (config.deviceSpecs) log.info(`Device specs : ${JSON.stringify(config.deviceSpecs)}`);

if (args.stdio) {
  // stdio: single persistent server instance
  const server = await createServer(config);
  const { StdioServerTransport } = await import('@modelcontextprotocol/sdk/server/stdio.js');
  const transport = new StdioServerTransport();
  await server.connect(transport);
  log.info('MCP server running on stdio');
}

if (args.http) {
  const port = parseInt(args.http, 10);
  config.httpPort = port;  // so MCP tools can include stream URLs in responses
  globalThis.__xioHttpPort = port; // used by job-manager for screenshot URLs
  const { startHttpServer } = await import('../src/http-transport.mjs');
  const { getRunningJob }   = await import('../src/core/job-manager.mjs');
  // The factory is called ONCE to create a singleton McpServer reused across all requests.
  // Only the StreamableHTTPServerTransport is fresh per POST /mcp (stateless transport layer).
  // Also passes getRunningJob so the MJPEG screencaster can reach the live page.
  await startHttpServer(() => createServer(config), port, { getRunningJob });
  log.info(`MCP server running on HTTP port ${port}`);
}
