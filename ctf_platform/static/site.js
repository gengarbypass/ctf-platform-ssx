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

for (const input of document.querySelectorAll("[data-image-form] [data-image-input]")) {
  const form = input.form;
  const editor = form.closest("[data-brand-editor]") || form;
  const preview = editor.querySelector(`[data-image-preview="${input.name}"]`);
  const remove = form.querySelector(`[data-remove-image="${input.name}"]`);
  const placeholder = preview.parentElement.querySelector("[data-preview-icon]");
  const frame = preview.closest("[data-image-frame]");
  const original = preview.getAttribute("src");
  let previewUrl;
  function updateImage() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = input.files.length ? URL.createObjectURL(input.files[0]) : null;
    const url = previewUrl || (remove?.checked ? preview.dataset.imageFallback : original);
    if (url) preview.src = url;
    else preview.removeAttribute("src");
    preview.hidden = !url;
    if (placeholder) placeholder.hidden = Boolean(url);
    frame?.classList.toggle("has-image", Boolean(url));
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
  // 行数下限：默认至少保留 1 行；容器声明 data-min-rows="0" 时允许删到空（如 AWDP 靶机的靶点）
  const minimum = Number(breachRows.dataset.minRows ?? 1);
  const rows = () => breachRows.querySelectorAll(".arena-breach-row");
  function renumberBreachRows() {
    rows().forEach((row, index) => {
      row.querySelector("[data-breach-row-label]").textContent = `靶点 ${index + 1}`;
      row.querySelector("[data-breach-remove]").disabled = rows().length <= minimum;
    });
  }
  document.querySelector("[data-breach-add]")?.addEventListener("click", () => {
    breachRows.appendChild(template.content.cloneNode(true));
    renumberBreachRows();
    const added = rows()[rows().length - 1];
    (added.querySelector("input[name='layer']") || added.querySelector("input:not([type=hidden])"))?.focus();
  });
  breachRows.addEventListener("click", event => {
    const remove = event.target.closest("[data-breach-remove]");
    if (remove && !remove.disabled && rows().length > minimum) {
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

for (const form of document.querySelectorAll("form[data-bulk-delete]")) {
  const controls = [...form.elements];
  const boxes = controls.filter(input => input.type === "checkbox" && input.name === "select" && !input.disabled);
  const selectAll = controls.find(input => input.hasAttribute("data-select-all"));
  const button = form.querySelector("[data-bulk-delete-button]");
  const sync = () => {
    const selected = boxes.filter(box => box.checked).length;
    button.disabled = selected === 0;
    if (selectAll) {
      selectAll.disabled = boxes.length === 0;
      selectAll.checked = selected > 0 && selected === boxes.length;
      selectAll.indeterminate = selected > 0 && selected < boxes.length;
    }
  };
  boxes.forEach(box => box.addEventListener("change", sync));
  selectAll?.addEventListener("change", () => {
    for (const box of boxes) box.checked = selectAll.checked;
    sync();
  });
  sync();
}

const coverInput = document.querySelector("[data-cover-input]");
const cropEditor = document.querySelector("[data-crop-editor]");
if (coverInput && cropEditor) {
  const canvas = cropEditor.querySelector("[data-crop-canvas]");
  const current = document.querySelector("[data-cover-current]");
  const fields = {};
  for (const name of ["crop_x", "crop_y", "crop_w", "crop_h"]) fields[name] = cropEditor.querySelector(`input[name='${name}']`);
  const view = { scale: 1, x: 0, y: 0 };
  let bitmap = null;
  let drag = null;

  function clampView() {
    const box = cropEditor.getBoundingClientRect();
    view.x = Math.min(0, Math.max(box.width - bitmap.width * view.scale, view.x));
    view.y = Math.min(0, Math.max(box.height - bitmap.height * view.scale, view.y));
  }
  function render() {
    const width = Math.round(bitmap.width * view.scale);
    const height = Math.round(bitmap.height * view.scale);
    canvas.width = width;
    canvas.height = height;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    canvas.style.left = `${view.x}px`;
    canvas.style.top = `${view.y}px`;
    canvas.getContext("2d").drawImage(bitmap, 0, 0, width, height);
  }
  function layout() {
    const box = cropEditor.getBoundingClientRect();
    view.scale = Math.max(box.width / bitmap.width, box.height / bitmap.height);
    view.x = (box.width - bitmap.width * view.scale) / 2;
    view.y = (box.height - bitmap.height * view.scale) / 2;
    clampView();
    render();
    writeFields();
  }
  function writeFields() {
    const box = cropEditor.getBoundingClientRect();
    fields.crop_x.value = Math.round(-view.x / view.scale);
    fields.crop_y.value = Math.round(-view.y / view.scale);
    fields.crop_w.value = Math.round(box.width / view.scale);
    fields.crop_h.value = Math.round(box.height / view.scale);
  }
  async function loadCover(file) {
    cropEditor.hidden = false;
    if (current) current.hidden = true;
    bitmap = await createImageBitmap(file);
    layout();
  }
  coverInput.addEventListener("change", () => {
    if (coverInput.files[0]) loadCover(coverInput.files[0]);
  });
  canvas.addEventListener("pointerdown", event => {
    event.preventDefault();
    drag = { startX: event.clientX, startY: event.clientY, baseX: view.x, baseY: view.y };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", event => {
    if (!drag) return;
    view.x = drag.baseX + event.clientX - drag.startX;
    view.y = drag.baseY + event.clientY - drag.startY;
    clampView();
    render();
    writeFields();
  });
  canvas.addEventListener("pointerup", () => { drag = null; });
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
