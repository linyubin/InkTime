"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const script = fs.readFileSync(path.join(__dirname, "../static/admin_csrf.js"), "utf8");
function boot(cookie = "__Host-inktime-csrf=nonce.signature") {
  const calls = [];
  const listeners = {};
  const document = {
    cookie,
    addEventListener: (name, callback) => { listeners[name] = callback; },
    createElement: () => ({})
  };
  const window = {
    location: {href: "https://inktime.example/manager", origin: "https://inktime.example"},
    fetch: (...args) => { calls.push(args); return Promise.resolve({ok: true}); }
  };
  vm.runInNewContext(script, {window, document, URL, Headers, Request});
  return {window, calls, listeners};
}

test("POST and DELETE same-origin fetch carry signed cookie token", async () => {
  const {window, calls} = boot();
  await window.fetch("/api/select/push", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
  await window.fetch("/api/styles/P05", {method: "DELETE"});
  assert.equal(calls.length, 2);
  assert.equal(calls[0][1].headers.get("X-InkTime-CSRF"), "nonce.signature");
  assert.equal(calls[0][1].headers.get("Content-Type"), "application/json");
  assert.equal(calls[1][1].headers.get("X-InkTime-CSRF"), "nonce.signature");
});

test("GET and cross-origin POST never leak CSRF header", async () => {
  const {window, calls} = boot();
  await window.fetch("/api/photos");
  await window.fetch("https://elsewhere.example/api", {method: "POST"});
  assert.equal(calls[0][1], undefined);
  assert.equal(calls[1][1].headers, undefined);
});

test("Request objects and missing cookie preserve fail-closed semantics", async () => {
  const a = boot();
  await a.window.fetch(new Request("https://inktime.example/api/styles", {method: "POST", body: "x"}));
  assert.equal(a.calls[0][1].headers.get("X-InkTime-CSRF"), "nonce.signature");
  const b = boot("");
  await b.window.fetch("/api/styles", {method: "POST"});
  assert.equal(b.calls[0][1].headers, undefined);
});

test("same-origin HTML forms receive a hidden CSRF field", () => {
  const {listeners} = boot();
  let field;
  const form = {
    tagName: "FORM", method: "post", action: "https://inktime.example/api/device/config",
    querySelector: () => field,
    appendChild: value => { field = value; }
  };
  listeners.submit({target: form});
  assert.equal(field.name, "csrf_token");
  assert.equal(field.value, "nonce.signature");
});
