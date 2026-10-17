"use strict";
const form = document.getElementById("login-form");
const password = document.getElementById("access-password");
const toggle = document.getElementById("toggle-password");
const button = document.getElementById("login-button");
const error = document.getElementById("login-error");
toggle.addEventListener("click", () => {
  const visible = password.type === "password";
  password.type = visible ? "text" : "password";
  toggle.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
  toggle.setAttribute("aria-pressed", String(visible));
});
password.addEventListener("input", () => { error.hidden = true; password.removeAttribute("aria-invalid"); });
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (button.disabled) return;
  button.disabled = true;
  button.querySelector(".login-button-text").textContent = "正在进入…";
  error.hidden = true;
  try {
    const response = await fetch("/auth/login", {
      method: "POST", mode: "same-origin", referrerPolicy: "same-origin", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json" }, signal: AbortSignal.timeout(12000),
      body: JSON.stringify({ password: password.value, remember: document.getElementById("remember-login").checked })
    });
    let result;
    try { result = await response.json(); }
    catch { throw new Error(response.status === 403 ? "请求校验未通过，请刷新页面后重试。" : "服务暂时不可用，请稍后重试。"); }
    if (!response.ok) throw new Error(result.message || "暂时无法登录，请稍后重试。");
    password.value = "";
    window.location.replace("/");
  } catch (failure) {
    error.textContent = failure instanceof TypeError || failure.name === "TimeoutError" ? "暂时无法连接光匣，请稍后重试。" : failure.message;
    error.hidden = false;
    password.setAttribute("aria-invalid", "true");
  } finally {
    button.disabled = false;
    button.querySelector(".login-button-text").textContent = "进入光匣";
  }
});
