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

const login = document.querySelector("[data-login-presentation]");
if (login) {
  const cover = login.querySelector("[data-login-cover]");
  const image = cover.querySelector("img");
  const art = cover.querySelector(".arena-art");
  const date = cover.querySelector("[data-event-date]");
  async function updatePresentation() {
    try {
      const response = await fetch(login.dataset.loginPresentation, { cache: "no-store", signal: AbortSignal.timeout(6500) });
      if (!response.ok) return;
      const event = await response.json();
      cover.querySelector("[data-event-title]").textContent = event.title;
      cover.querySelector("[data-event-summary]").textContent = event.summary;
      cover.querySelector("[data-event-status]").textContent = event.status;
      cover.querySelector("[data-event-schedule]").textContent = event.schedule;
      date.hidden = !event.schedule;
      cover.classList.toggle("has-image", Boolean(event.cover));
      if (event.cover && image.getAttribute("src") !== event.cover) image.src = event.cover;
      if (!event.cover) image.removeAttribute("src");
      image.hidden = !event.cover;
      art.hidden = Boolean(event.cover);
    } catch {
      // Leave the last successful presentation visible during temporary disconnections.
    } finally {
      setTimeout(updatePresentation, 8000);
    }
  }
  setTimeout(updatePresentation, 8000);
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
  mode.addEventListener("change", updateRoundDuration);
  updateRoundDuration();
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
