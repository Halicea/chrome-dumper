# chrome-dumper plugins

Plugins extend the extension with **new command types** and **in-page UI**,
reusing the existing bridge transport. A plugin can have two halves: an **extension half** (browser primitives + UI,
under `extension/plugins/<id>/`) and an optional **client half** (data/logic,
under `client/src/chrome_dumper_client/`). The extension half self-registers at
service-worker startup, the same way `debugger.js` does. The bridge stays a pure
relay between them.

```
plugins/
  loader.js              # the SDK: registry + panel host + plugin_action bridge
  <id>/
    commands.js          # required: calls self.pluginRegistry.register({...})
    plugin.json          # optional: human-readable manifest (not executed)
```

## How registration works

`background.js` does `importScripts("plugins/loader.js")` during the SW's
synchronous startup. `loader.js` defines `self.pluginRegistry`, then
`importScripts`-es each plugin's `commands.js`, which registers itself:

```js
self.pluginRegistry.register({
  id: "my-plugin",
  name: "My plugin",
  match: ["*://*.example.com/*"],          // url globs (used by panels/UX)
  panel: { panelId, html, css },           // optional in-page UI
  commands: {
    my_command: async (msg, ctx) => {      // msg = the JSON sent to /cmd
      const tab = await ctx.getTargetTab(msg);
      // ...do work via ctx.exec / chrome.* ...
      return { type: "my_result", ok: true, value: 42 };  // → reply to client
    },
  },
});
```

A command's return object is merged into the bridge reply (`{ id, ...return }`),
exactly like the built-in commands. The **bridge needs no changes** — unknown
command types flow straight through to the extension, where the registry
dispatches them before the built-in `switch`.

> **MV3 constraint:** service workers can only `importScripts` files packaged in
> the extension, and only during synchronous startup. So plugins are discovered
> from the **static list at the bottom of `loader.js`**, not the filesystem.
> Adding a plugin = create the folder, add one `importScripts` line, **reload the
> extension**. There is no runtime install.

## The `ctx` handed to handlers

| field | what |
|---|---|
| `ctx.getTargetTab(msg)` | resolve `msg.tabId` or the active tab |
| `ctx.waitForComplete(tabId, ms?)` | await a tab navigation completing |
| `ctx.exec(opts)` | `chrome.scripting.executeScript(opts)` (use `world:"MAIN"` for page-context JS) |
| `ctx.chrome` | the `chrome` API |
| `ctx.registry` | the plugin registry (introspection) |

## In-page UI

`self.mountPluginPanel(tabId, { plugin, panelId, html, css })` injects a
shadow-DOM overlay. An element with `data-plugin-status` shows results.
`self.unmountPluginPanel(tabId, panelId)` removes it. A click on
`data-plugin-action="foo"` routes one of two ways:

- if a command `${plugin}_foo` **is registered** → it runs in the SW
  (extension-handled — pure browser actions, e.g. closing the panel);
- otherwise → the loader emits an unsolicited **`plugin_event`** over the bridge
  (`{type:"plugin_event", plugin, action, tabId}`) for the **client half** to
  handle. The client does the data work and calls back with primitive commands.

## Client half (optional)

For logic that needs the local filesystem or other host resources, add a Python
module under `client/src/chrome_dumper_client/` mirroring `debug.py`: a
`register(sub)` that adds argparse subcommands and a `dispatch(args, d)`. Wire
both into `__main__.py` (import, `…_module.register(sub)`,
`if …_module.dispatch(args, d): return`). A long-running subcommand can
subscribe to the event rail via `_sse_events(d.base_url, None, d.session)` and
react to its plugin's `plugin_event`s — this is how an "always-on" client turns
a panel button into a server-free action.

## Invoking plugin commands

Through the normal bridge — no client changes needed:

```bash
curl -s 127.0.0.1:8766/cmd -H 'content-type: application/json' \
  -d '{"type":"list_plugins"}'
curl -s 127.0.0.1:8766/cmd -H 'content-type: application/json' \
  -d '{"type":"messaging_scan"}'
```

Add `?session=<id|name>` when more than one browser is connected.

## Bundled plugins

- **messaging** — LinkedIn Recruiter InMail draft-assist. Two halves: the
  extension exposes primitives (`messaging_scan`, `inject_value`,
  `messaging_set_status`, `messaging_mount`/`unmount`) + the in-drawer panel; the
  **client half** (`client/.../messaging.py`) reads the local `sourcing/` repo,
  matches the candidate, renders the job's template, and injects the draft — no
  extra API server. Run `dumper messaging watch` (always-on) to make the panel's
  "Insert draft" button work, or `dumper messaging insert` one-shot. Needs
  `$SOURCING_ROOT`. **Never sends** — the recruiter reviews and clicks Send.
