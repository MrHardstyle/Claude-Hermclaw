// Hidden checks of task A2 (contact form). Run from the delivered work tree:
//   node --test --test-reporter=tap _hidden/hidden_a2.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { validate, validateName, validateEmail, validateAge, validateMessage, attachForm } from "../form.js";

const FIELDS = ["name", "email", "age", "message"];
const VALID = { name: "Anna", email: "anna@example.org", age: "30", message: "Hallo" };

test("name: trimmed length of at least 2", () => {
  assert.notEqual(validateName(""), null);
  assert.notEqual(validateName("   "), null);
  assert.notEqual(validateName(" A "), null);
  assert.notEqual(validateName("A"), null);
  assert.equal(validateName("Al"), null);
  assert.equal(validateName(" Al "), null);
});

test("email: part before and after @, domain with a dot", () => {
  assert.notEqual(validateEmail(""), null);
  assert.notEqual(validateEmail("anna@example"), null);
  assert.notEqual(validateEmail("@example.org"), null);
  assert.notEqual(validateEmail("anna@"), null);
  assert.notEqual(validateEmail("anna.example.org"), null);
  assert.equal(validateEmail("anna@example.org"), null);
  assert.equal(validateEmail("a.b@mail.example.de"), null);
});

test("age: optional whole number from 16 to 120", () => {
  assert.equal(validateAge(""), null);
  assert.equal(validateAge("16"), null);
  assert.equal(validateAge("120"), null);
  for (const bad of ["15", "121", "17.5", "abc", "100abc", "-20", "0"]) {
    assert.notEqual(validateAge(bad), null, `age ${JSON.stringify(bad)} must be rejected`);
  }
});

test("message: required, at most 500 characters", () => {
  assert.notEqual(validateMessage(""), null);
  assert.equal(validateMessage("x"), null);
  assert.equal(validateMessage("x".repeat(500)), null);
  assert.notEqual(validateMessage("x".repeat(501)), null);
});

test("validate collects the error of every invalid field", () => {
  assert.deepEqual(validate(VALID), {});
  const errors = validate({ name: "A", email: "x", age: "200", message: "" });
  assert.deepEqual(Object.keys(errors).sort(), [...FIELDS].sort());
  for (const field of FIELDS) assert.equal(typeof errors[field], "string");
});

// A stand-in for the DOM form of index.html: values are read with `new FormData(form)` (or form.elements),
// errors go to `[data-error-for=<field>]`, the success message to `#result`.
function fakeForm() {
  const values = { ...VALID };
  const errors = Object.fromEntries(FIELDS.map((f) => [f, { textContent: "", innerText: "", innerHTML: "" }]));
  const result = { textContent: "", innerText: "", innerHTML: "" };
  const inputs = Object.fromEntries(FIELDS.map((f) => [f, { name: f, get value() { return values[f]; } }]));
  const handlers = [];
  const form = {
    values,
    errors,
    result,
    elements: Object.assign(Object.values(inputs), inputs, { namedItem: (n) => inputs[n] }),
    addEventListener(type, handler) { if (type === "submit") handlers.push(handler); },
    set onsubmit(handler) { handlers.push(handler); },
    querySelector(sel) {
      if (sel === "#result") return result;
      const err = sel.match(/data-error-for=["']?(\w+)/);
      if (err) return errors[err[1]] ?? null;
      const named = sel.match(/name=["']?(\w+)/) || sel.match(/^#(\w+)$/);
      if (named) return inputs[named[1]] ?? null;
      return null;
    },
    querySelectorAll(sel) {
      if (/data-error-for|\.error/.test(sel) && !/data-error-for=/.test(sel)) return Object.values(errors);
      const one = form.querySelector(sel);
      return one ? [one] : [];
    },
    submit() {
      let prevented = false;
      const event = { type: "submit", target: form, currentTarget: form, preventDefault() { prevented = true; } };
      for (const h of handlers) h.call(form, event);
      return prevented;
    },
  };
  return form;
}

globalThis.FormData = class {
  constructor(form) { this.form = form; }
  entries() { return Object.entries(this.form.values)[Symbol.iterator](); }
  [Symbol.iterator]() { return this.entries(); }
  get(name) { return this.form.values[name] ?? null; }
};
globalThis.document ??= { getElementById: () => null, querySelector: () => null };

const text = (el) => el.textContent || el.innerText || el.innerHTML || "";

test("submitting does not reload the page", () => {
  const form = fakeForm();
  attachForm(form);
  assert.equal(form.submit(), true, "the submit handler must call event.preventDefault()");
});

test("a failed submit shows the errors and no success message", () => {
  const form = fakeForm();
  attachForm(form);
  Object.assign(form.values, { name: "A", email: "x" });
  form.submit();
  assert.notEqual(text(form.errors.name), "");
  assert.notEqual(text(form.errors.email), "");
  assert.equal(text(form.errors.message), "");
  assert.equal(text(form.result), "");
});

test("errors of an earlier submit are cleared", () => {
  const form = fakeForm();
  attachForm(form);
  Object.assign(form.values, { name: "A", email: "x" });
  form.submit();
  Object.assign(form.values, { name: "Anna", email: "anna@example.org" });
  form.submit();
  for (const field of FIELDS) assert.equal(text(form.errors[field]), "", `old error of ${field} still shown`);
  assert.notEqual(text(form.result), "", "the success message is missing");
});

test("the own tests cover the rules and pass", () => {
  const files = readdirSync("tests", { recursive: true })
    .map(String)
    .filter((f) => /\.(c|m)?js$/.test(f))
    .map((f) => join("tests", f));
  assert.ok(files.length > 0, "no test files under tests/");
  const env = { ...process.env };
  delete env.NODE_TEST_CONTEXT; // a nested runner would otherwise report to this one instead of printing TAP
  const run = spawnSync(process.execPath, ["--test", "--test-reporter=tap", ...files], { encoding: "utf8", env, timeout: 120000 });
  assert.equal(run.status, 0, run.stdout.slice(-2000) + run.stderr.slice(-1000));
  const passed = Number((run.stdout.match(/^# pass (\d+)/m) || [])[1] || 0);
  assert.ok(passed >= 6, `only ${passed} own tests; the job asks for the edge cases of every rule`);
});

test("the README rules are unchanged", () => {
  assert.equal(readFileSync("README.md", "utf8"), readFileSync("_hidden/_base/README.md", "utf8"));
});
