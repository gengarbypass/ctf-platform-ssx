"use strict";

function displayDuration(seconds, hours = false) {
  const minutes = Math.floor(seconds / 60);
  const pad = number => String(number).padStart(2, "0");
  return hours ? `${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}:${pad(seconds % 60)}` :
    `${pad(minutes)}:${pad(seconds % 60)}`;
}

for (const clock of document.querySelectorAll("[data-clock-end]")) {
  const end = Number(clock.dataset.clockEnd);
  const origin = Number(clock.dataset.serverNow);
  if (end <= origin) continue;
  const started = performance.now();
  const timer = setInterval(() => {
    const remaining = Math.max(0, end - origin - Math.floor((performance.now() - started) / 1000));
    clock.textContent = displayDuration(remaining);
    if (remaining === 0) {
      clearInterval(timer);
      window.location.reload();
    }
  }, 1000);
}

for (const dialog of document.querySelectorAll("[data-auto-dialog]")) {
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.open = true;
  dialog.addEventListener("close", () => document.querySelector('input[name="flag"]')?.focus());
}

for (const form of document.querySelectorAll("[data-image-form]")) {
  const editor = form.closest("[data-brand-editor]");
  const preview = (editor || form).querySelector("[data-image-preview]");
  const input = form.querySelector("[data-image-input]");
  const remove = form.querySelector("[data-remove-image]");
  const placeholder = editor?.querySelector("[data-preview-icon]");
  const original = preview.getAttribute("src");
  let previewUrl;
  function updateImage() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = input.files.length ? URL.createObjectURL(input.files[0]) : null;
    const url = previewUrl || (remove?.checked ? null : original);
    if (url) preview.src = url;
    else preview.removeAttribute("src");
    preview.hidden = !url;
    if (placeholder) placeholder.hidden = Boolean(url);
  }
  input.addEventListener("change", updateImage);
  remove?.addEventListener("change", updateImage);
}

const contestForm = document.querySelector("[data-contest-form]");
const roundDuration = contestForm?.querySelector("[data-round-duration]");
if (roundDuration) {
  const mode = contestForm.elements.namedItem("mode");
  const minutes = contestForm.elements.namedItem("round_minutes");
  function updateRoundDuration() {
    const isRound = mode.value === "round";
    roundDuration.hidden = !isRound;
    minutes.disabled = !isRound;
    minutes.required = isRound;
  }
  for (const choice of contestForm.querySelectorAll('input[name="mode"]')) {
    choice.addEventListener("change", updateRoundDuration);
  }
  updateRoundDuration();
}

const breachRows = document.querySelector("[data-breach-rows]");
if (breachRows) {
  const template = breachRows.querySelector("template[data-breach-row]");
  const rows = () => breachRows.querySelectorAll(".arena-breach-row");
  function renumberBreachRows() {
    rows().forEach((row, index) => {
      row.querySelector("[data-breach-row-label]").textContent = `靶点 ${index + 1}`;
      row.querySelector("[data-breach-remove]").disabled = rows().length <= 1;
    });
  }
  document.querySelector("[data-breach-add]")?.addEventListener("click", () => {
    breachRows.appendChild(template.content.cloneNode(true));
    renumberBreachRows();
    const added = rows()[rows().length - 1];
    added.querySelector("input[name='layer']").focus();
  });
  breachRows.addEventListener("click", event => {
    const remove = event.target.closest("[data-breach-remove]");
    if (remove && !remove.disabled && rows().length > 1) {
      remove.closest(".arena-breach-row").remove();
      renumberBreachRows();
    }
  });
  renumberBreachRows();
}

const probeBox = document.querySelector("[data-env-probe]");
if (probeBox) {
  const button = probeBox.querySelector("[data-probe-button]");
  const result = probeBox.querySelector("[data-probe-result]");
  const form = button.closest("form");
  button.addEventListener("click", async () => {
    const image = form.elements.namedItem("env_image").value.trim();
    const port = form.elements.namedItem("env_container_port").value || "80";
    const tokenInput = form.elements.namedItem("csrf_token");
    button.disabled = true;
    result.classList.remove("arena-positive", "arena-negative");
    result.textContent = "正在检测：启动一次性容器并尝试连接端口（约需数秒）…";
    try {
      const response = await fetch("/manage/bank/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": tokenInput ? tokenInput.value : "" },
        body: JSON.stringify({ env_image: image, env_container_port: port }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      result.textContent = data.message;
      result.classList.add(data.ok ? "arena-positive" : "arena-negative");
    } catch {
      result.textContent = "检测请求失败，请稍后重试。";
      result.classList.add("arena-negative");
    } finally {
      button.disabled = false;
    }
  });
}

const bulkDelete = document.querySelector("[data-bulk-delete]");
if (bulkDelete) {
  const boxes = [...bulkDelete.querySelectorAll('input[name="select"]')];
  const button = bulkDelete.querySelector("[data-bulk-delete-button]");
  const sync = () => {
    button.disabled = !boxes.some(box => box.checked);
  };
  boxes.forEach(box => box.addEventListener("change", sync));
  bulkDelete.querySelector("[data-select-all]")?.addEventListener("change", event => {
    for (const box of boxes) if (!box.disabled) box.checked = event.target.checked;
    sync();
  });
  sync();
}

const editor = document.querySelector("[data-brand-editor]");
if (editor) {
  const preview = editor.querySelector("[data-brand-preview]");
  const fields = editor.querySelector("form").elements;
  function updateBrand() {
    preview.querySelector("[data-preview-name]").textContent = fields.name.value || "比赛平台";
    preview.querySelector("[data-preview-icon]").textContent = { flag: "⚑", shield: "◇", code: "‹›" }[fields.icon.value];
    for (const [field, property] of [["accent", "--preview-blue"], ["background", "--preview-bg"], ["surface", "--preview-paper"]]) {
      preview.style.setProperty(property, fields[field].value);
      editor.querySelector(`[data-color-value="${field}"]`).textContent = fields[field].value.toUpperCase();
    }
    preview.style.setProperty("--preview-radius", `${Math.min(24, Math.max(4, Number(fields.radius.value)))}px`);
  }
  editor.addEventListener("input", updateBrand);
  editor.addEventListener("change", updateBrand);
}
