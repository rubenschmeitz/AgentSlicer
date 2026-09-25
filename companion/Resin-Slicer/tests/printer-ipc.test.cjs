const test = require("node:test");
const assert = require("node:assert/strict");
const { buildPrinterArgs } = require("../electron/printer-ipc");

test("only enumerated operations, separate print command and literal argument arrays", () => {
  assert.throws(() => buildPrinterArgs("delete", {}));
  assert.throws(() => buildPrinterArgs("status", {address: "192.168.1.1; calc"}));
  assert.throws(() => buildPrinterArgs("upload", {address: "192.168.1.114"}));
  const args = buildPrinterArgs("upload", {address: "192.168.1.114", project: "C:\\My models\\job"});
  assert.ok(args.includes("C:\\My models\\job"));
  assert.ok(!args.includes("--confirm-start"));
  assert.deepEqual(buildPrinterArgs("print", {receipt: "C:\\receipt.json"}),
    ["-m", "agent_slicer.cli", "printer", "print", "C:\\receipt.json", "--confirm-start"]);
});
