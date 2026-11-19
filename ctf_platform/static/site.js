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
const envConfig = contestForm?.querySelector("[data-env-config]");
if (roundDuration || envConfig) {
  const mode = contestForm.elements.namedItem("mode");
  const minutes = contestForm.elements.namedItem("round_minutes");
  const breachOnly = envConfig ? [...envConfig.querySelectorAll("[data-env-breach-only] input")] : [];
  const sharedFields = envConfig ? [...envConfig.querySelectorAll("input")] : [];
  const shared = sharedFields.filter(field => !breachOnly.includes(field));
  function updateModeSections() {
    if (roundDuration) {
      const isRound = mode.value === "round";
      roundDuration.hidden = !isRound;
      minutes.disabled = !isRound;
      minutes.required = isRound;
    }
    if (envConfig) {
      const isBreach = mode.value === "breach";
      const isDynamic = isBreach || mode.value === "puzzle";
      envConfig.hidden = !isDynamic;
      for (const field of breachOnly) {
        field.disabled = !isBreach;
        field.required = isBreach && field.name !== "env_image";
      }
      for (const field of shared) {
        field.disabled = !isDynamic;
        field.required = isBreach;
      }
    }
  }
  for (const choice of contestForm.querySelectorAll('input[name="mode"]')) {
    choice.addEventListener("change", updateModeSections);
  }
  updateModeSections();
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
