const panel = document.getElementById("lanPrinterPanel");
const output = document.getElementById("lanPrinterStatus");
const picker = document.getElementById("lanPrinterSelect");
const address = document.getElementById("lanPrinterAddress");
let devices = [], selected = null, project = null, receipt = null;
let busy = false;
const buttons = [...panel.querySelectorAll("button[data-action]")];

function controls() {
  for (const button of buttons) {
    const action = button.dataset.action;
    button.disabled = busy || (["status", "files", "upload"].includes(action) && !selected)
      || (action === "upload" && !project) || (action === "print" && !receipt);
  }
  picker.disabled = busy;
  address.disabled = busy;
}
async function run(action) {
  if (busy) return;
  busy = true; controls();
  output.textContent = action === "upload" ? "Uploading validated file… this can take several minutes." : "Working…";
  try {
    if (action === "project") {
      project = await window.slicer.choosePrinterProject();
      receipt = null;
      document.getElementById("lanPrinterProject").textContent = project || "No project selected";
      output.textContent = "Upload checks the completed build, production validation and file hash. Draft files are blocked.";
      return;
    }
    if (action === "upload") receipt = null;
    const payload = action === "discover" ? { address: address.value.trim() || undefined }
      : action === "print" ? { receipt } : { ...selected, project };
    const result = await window.slicer.printerAction(action, payload);
    if (action === "discover") {
      devices = result.printers || []; selected = devices[0] || null; receipt = null;
      picker.replaceChildren(...devices.map((device, index) => new Option(
        device.name + " · " + device.address + " · " + device.board_id, String(index))));
      if (!selected) picker.add(new Option("No SDCP printer found", ""));
    }
    if (action === "upload") receipt = result.receipt || null;
    if (action === "print" && result.status !== "cancelled") receipt = null;
    if (action === "status") {
      output.textContent = (result.device?.name || "Printer") + " — " + (result.idle ? "IDLE" : "BUSY")
        + "\nAddress: " + result.device?.address + "\nFirmware: " + result.Attributes?.FirmwareVersion
        + "\nResolution: " + result.Attributes?.Resolution
        + "\nFormats: " + (result.Attributes?.SupportFileType || []).join(", ")
        + "\n\n" + (result.warnings || []).join("\n");
    } else if (action === "discover") {
      output.textContent = devices.length ? "Found " + devices.length + " printer(s). Select one and refresh status."
        : "No printer found. Check power, Wi-Fi/LAN, or enter its IPv4 address and try again.";
    } else if (action === "files") {
      output.textContent = (result.files || []).map(file => file.name).join("\n") || "No printable files found.";
    } else if (action === "upload") {
      output.textContent = "Uploaded: " + result.filename + "\nPrint has NOT started."
        + "\nReceipt: " + result.receipt + "\nVerification: " + result.verification;
    } else {
      output.textContent = result.status === "cancelled" ? "Cancelled. Print was not started."
        : "Printer acknowledged the start request. Check the printer's progress.";
    }
  } catch (error) { output.textContent = error.message || String(error); }
  finally { busy = false; controls(); }
}
for (const button of buttons) button.addEventListener("click", () => run(button.dataset.action));
picker.addEventListener("change", () => { selected = devices[Number(picker.value)] || null; receipt = null; controls(); });
document.getElementById("lanPrinterButton").addEventListener("click", () => panel.showModal());
document.getElementById("lanPrinterClose").addEventListener("click", () => panel.close());
controls();
