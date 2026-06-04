// Lets the user name this profile's session. The name is stored in
// chrome.storage.local; background.js watches for the change and re-announces
// the session to the bridge, so clients can target it with --session <name>.
const nameEl = document.getElementById("name");
const statusEl = document.getElementById("status");
const sidEl = document.getElementById("sid");

chrome.storage.local.get(["sessionId", "sessionName"], (got) => {
  nameEl.value = got.sessionName || "";
  sidEl.textContent = got.sessionId
    ? `id: ${got.sessionId}`
    : "id: (assigned on first connect)";
});

function save() {
  const name = nameEl.value.trim();
  chrome.storage.local.set({ sessionName: name }, () => {
    statusEl.textContent = "saved";
    setTimeout(() => { statusEl.textContent = ""; }, 1500);
  });
}

document.getElementById("save").addEventListener("click", save);
nameEl.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
