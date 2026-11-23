/* Printvault — last script on every page: form behaviour and the sidebar status. No inline scripts (CSP). */
(function () {
  "use strict";
  document.querySelectorAll("input[data-autosubmit]").forEach(input =>
    input.addEventListener("change", () => input.form.submit()));
  document.querySelectorAll("button[data-back]").forEach(button =>
    button.addEventListener("click", () => history.back()));

  // print form: the fail fields only with status 'failed'; 'other' needs a note
  const status = document.querySelector('select[name="status"]'), box = document.getElementById("fail-fields");
  if (status && box) {
    const reason = document.getElementById("fail-reason"), note = document.getElementById("fail-note");
    const sync = () => {
      box.hidden = status.value !== "failed";
      note.required = !box.hidden && reason.value === "other";
    };
    status.addEventListener("change", sync);
    reason.addEventListener("change", sync);
    sync();
  }

  // printer form: the camera choice only with model 'other'
  const model = document.getElementById("bambu-model"), camera = document.getElementById("camera-type-field");
  if (model && camera) {
    const sync = () => { camera.hidden = model.value !== "other"; };
    model.addEventListener("change", sync);
    sync();
  }

  PV.boot();
})();
