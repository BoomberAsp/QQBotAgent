const statusLine = document.querySelector("#status");
const confirmation = document.querySelector("#confirm");
const form = document.querySelector("#login");
const account = document.querySelector("#account");
const password = document.querySelector("#password");
const button = document.querySelector("#submit");
const savedLogin = document.querySelector("#saved-login");
const savedAccount = document.querySelector("#saved-account");
const resume = document.querySelector("#resume");

function post(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
}

async function showState(response) {
  const data = await response.json();
  if (!response.ok) {
    statusLine.textContent = data.message;
    confirmation.hidden = true;
    return;
  }
  if (data.state === "ready") {
    statusLine.textContent = "已核对 QQ 身份。";
    confirmation.hidden = true;
    form.hidden = false;
    if (data.saved_login) {
      const saved = data.saved_login.account;
      savedAccount.textContent = saved || "已记住的游戏账号";
      if (saved && !account.value) account.value = saved;
      savedLogin.hidden = false;
    }
    return;
  }
  statusLine.textContent = "等待 QQ 确认。";
  confirmation.hidden = false;
  document.querySelector("#confirm-instructions").textContent = data.group
    ? "请回到发起登录的群，先 @ 机器人，再发送本页的核对命令。"
    : "请在发起登录的机器人私聊发送这条命令，再回到本页。";
  document.querySelector("#pin").textContent = `/璐茜玛 确认 ${data.pin}`;
  setTimeout(poll, 2200);
}

async function poll() {
  try {
    await showState(await fetch("/api/session", { cache: "no-store" }));
  } catch {
    statusLine.textContent = "暂时无法连接登录服务，正在重试。";
    setTimeout(poll, 3500);
  }
}

async function submitLogin(body, usingSaved) {
  if (button.disabled) return;
  button.disabled = true;
  resume.disabled = true;
  statusLine.textContent = usingSaved ? "正在恢复已保存的授权…" : "正在验证游戏账号…";
  try {
    const request = post("/api/login", body);
    password.value = "";
    const response = await request;
    statusLine.textContent = (await response.json()).message;
    if (usingSaved && response.status === 409) {
      savedLogin.hidden = true;
      password.focus();
    } else if (response.status !== 429 && !(usingSaved && response.status === 503)) {
      form.reset();
      form.hidden = true;
      savedLogin.hidden = true;
    }
  } catch {
    form.reset();
    form.hidden = true;
    savedLogin.hidden = true;
    statusLine.textContent = "结果暂时未知。请回 QQ 查询状态，必要时重新扫码。";
  } finally {
    button.disabled = false;
    resume.disabled = false;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  return submitLogin({
    account: account.value,
    password: password.value,
    remember: document.querySelector("#remember").checked,
  }, false);
});
resume.addEventListener("click", () => submitLogin({ use_saved: true }, true));

async function start() {
  const ticket = location.hash.slice(1);
  if (!ticket) return poll();
  history.replaceState(null, "", location.pathname);
  try {
    await showState(await post("/api/claim", { ticket }));
  } catch {
    statusLine.textContent = "暂时无法打开登录申请，请回 QQ 重新扫码。";
  }
}

start();
