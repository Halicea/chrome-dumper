// Firefox stub for the CDP / debug_* command surface.
//
// The Chrome build uses `chrome.debugger` (Chrome Debugger Protocol) to attach
// to a tab and stream Network/Fetch/Console events to the bridge. Firefox does
// not expose an equivalent WebExtensions API — `browser.debugger` does not
// exist — so every debug_* command here returns a structured error instead.
//
// The wire shape matches the Chrome build so the bridge / client can detect
// "this browser does not support debug commands" without crashing.

const NOT_SUPPORTED = {
  type: "error",
  error: "debug_not_supported_on_firefox",
};

async function handleDebugCommand(msg, _getTargetTab) {
  switch (msg.type) {
    case "debug_attach":
    case "debug_detach":
    case "debug_get_body":
    case "debug_pause_continue":
      return NOT_SUPPORTED;
    case "debug_status":
      // status is harmless — report empty attached list so callers can poll
      // without provoking an error path.
      return { type: "debug_status", attached: [], supported: false };
    default:
      return null;
  }
}

self.debugModule = { handleDebugCommand };
