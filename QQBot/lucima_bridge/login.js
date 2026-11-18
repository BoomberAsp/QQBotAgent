const statusLine = document.querySelector("#status");
const confirmation = document.querySelector("#confirm");
const form = document.querySelector("#login");
const account = document.querySelector("#account");
const password = document.querySelector("#password");
const button = document.querySelector("#submit");

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
    return;
  }
  statusLine.textContent = "等待 QQ 确认。";
  confirmation.hidden = false;
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

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (button.disabled) return;
  button.disabled = true;
  statusLine.textContent = "正在验证游戏账号…";
  try {
    const request = post("/api/login", {
      account: account.value,
      password: password.value,
      remember: document.querySelector("#remember").checked,
    });
    password.value = "";
    const response = await request;
    statusLine.textContent = (await response.json()).message;
    if (response.status !== 429) {
      form.reset();
      form.hidden = true;
    }
  } catch {
    form.reset();
    form.hidden = true;
    statusLine.textContent = "结果暂时未知。请回 QQ 查询状态，必要时重新扫码。";
  } finally {
    button.disabled = false;
  }
});

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
