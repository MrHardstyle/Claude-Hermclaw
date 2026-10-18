import { test } from "node:test";
import assert from "node:assert/strict";
import { validate, validateName } from "../form.js";

test("a complete entry has no errors", () => {
  assert.deepEqual(validate({ name: "Anna", email: "anna@example.org", age: "30", message: "Hallo" }), {});
});

test("the name is required", () => {
  assert.equal(validateName(""), "Bitte einen Namen angeben.");
});
