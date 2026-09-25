const path = require("path");
const { spawn } = require("child_process");
const fs = require("fs");

function buildPrinterArgs(action, payload = {}) {
  if (!["discover", "status", "files", "upload", "print"].includes(action)) throw new Error("Unknown printer action");
  const args = ["-m", "agent_slicer.cli", "printer", action];
  if (action === "print") {
    if (typeof payload.receipt !== "string" || !payload.receipt) throw new Error("Upload receipt required");
    return [...args, payload.receipt, "--confirm-start"];
  }
  if (action === "upload") {
    if (typeof payload.project !== "string" || !payload.project) throw new Error("Validated project required");
    args.push(payload.project);
  }
  if (payload.address) {
    if (typeof payload.address !== "string" || !/^\d{1,3}(\.\d{1,3}){3}$/.test(payload.address)) throw new Error("Enter a LAN IPv4 address");
    args.push("--address", payload.address);
  } else if (action !== "discover") throw new Error("Select a printer first");
  if (payload.board_id && action !== "discover") {
    if (!/^[A-Za-z0-9_-]{8,64}$/.test(payload.board_id)) throw new Error("Invalid printer identity");
    args.push("--board-id", payload.board_id);
  }
  return args;
}

function registerPrinterIPC({ ipcMain, dialog, BrowserWindow }) {
  const agentRoot = path.resolve(__dirname, "../../..");
  const python = process.env.AGENT_SLICER_PYTHON || path.join(agentRoot, ".venv", "Scripts", "python.exe");
  let busy = false;
  ipcMain.handle("printer:choose-project", async () => {
    const result = await dialog.showOpenDialog({ title: "Choose validated Agent-Slicer project", properties: ["openDirectory"] });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle("printer:action", async (event, { action, payload = {} } = {}) => {
    if (busy) throw new Error("A printer operation is already running");
    const args = buildPrinterArgs(action, payload);
    if (!fs.existsSync(python)) throw new Error("Agent-Slicer Python is missing. Install tools/agent-slicer, or set AGENT_SLICER_PYTHON.");
    busy = true;
    try {
      if (action === "print") {
        const owner = BrowserWindow.fromWebContents(event.sender);
        const result = await dialog.showMessageBox(owner, {
          type: "warning", title: "Start the physical resin printer?",
          message: "This will start the uploaded print and move the printer.",
          detail: "Confirm the build plate is clear and fitted, the vat/film is clean and undamaged, the correct resin is present, and the cover is closed.\n\nReceipt: " + payload.receipt,
          buttons: ["Cancel", "Start print"], defaultId: 0, cancelId: 0, noLink: true
        });
        if (result.response !== 1) return { status: "cancelled", print_started: false };
      }
      return await new Promise((resolve, reject) => {
        const child = spawn(python, args, { cwd: agentRoot, windowsHide: true, shell: false });
        let stdout = "", stderr = "", settled = false;
        const finish = (error, result) => {
          if (settled) return;
          settled = true; clearTimeout(timer);
          error ? reject(error) : resolve(result);
        };
        const timer = setTimeout(() => {
          child.kill();
          finish(new Error("Printer operation timed out. Check printer status before retrying; print start is never retried automatically."));
        }, action === "upload" ? 900000 : 45000);
        child.stdout.on("data", chunk => {
          stdout += chunk;
          if (stdout.length > 2000000) { child.kill(); finish(new Error("Printer response exceeded limit")); }
        });
        child.stderr.on("data", chunk => { stderr = (stderr + chunk).slice(-100000); });
        child.on("error", error => finish(error));
        child.on("close", code => {
          try {
            if (code !== 0) {
              let message = stderr || stdout || "Printer command failed";
              try { message = JSON.parse(message).message || message; } catch {}
              finish(new Error(message));
            } else finish(null, JSON.parse(stdout));
          } catch (error) { finish(error); }
        });
      });
    } finally { busy = false; }
  });
}

module.exports = { buildPrinterArgs, registerPrinterIPC };
