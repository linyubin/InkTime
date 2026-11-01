/* Browser-only CSRF adapter for same-origin administrator writes.
 * A signed token cookie is issued by Flask after successful Basic authentication.
 * The server rejects missing tokens; this script does not bypass authentication.
 */
(() => {
  "use strict";
  const cookieName = "__Host-inktime-csrf";
  const headerName = "X-InkTime-CSRF";
  const safeMethods = new Set(["GET", "HEAD", "OPTIONS"]);

  function csrfToken() {
    const item = document.cookie.split(";").map(s => s.trim())
      .find(s => s.startsWith(cookieName + "="));
    return item ? decodeURIComponent(item.slice(cookieName.length + 1)) : "";
  }

  const nativeFetch = window.fetch;
  if (typeof nativeFetch === "function") {
    window.fetch = function (input, init) {
      const opts = init ? {...init} : {};
      const isRequest = typeof Request !== "undefined" && input instanceof Request;
      const method = String(opts.method || (isRequest ? input.method : "GET")).toUpperCase();
      if (safeMethods.has(method)) return nativeFetch.call(this, input, init);

      const target = isRequest ? input.url : String(input);
      if (new URL(target, window.location.href).origin !== window.location.origin) {
        return nativeFetch.call(this, input, init);
      }
      const token = csrfToken();
      if (!token) return nativeFetch.call(this, input, init); // fail closed on server
      const headers = new Headers(opts.headers || (isRequest ? input.headers : undefined));
      headers.set(headerName, token);
      opts.headers = headers;
      return nativeFetch.call(this, input, opts);
    };
  }

  document.addEventListener("submit", event => {
    const form = event.target;
    if (!form || form.tagName !== "FORM" || safeMethods.has(form.method.toUpperCase())) return;
    if (new URL(form.action, window.location.href).origin !== window.location.origin) return;
    const token = csrfToken();
    if (!token) return;
    let field = form.querySelector('input[name="csrf_token"]');
    if (!field) {
      field = document.createElement("input");
      field.type = "hidden";
      field.name = "csrf_token";
      form.appendChild(field);
    }
    field.value = token;
  }, true);
})();
