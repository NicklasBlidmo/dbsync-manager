// Hook change form: the "Scheduled hooks only" fieldset is shown while
// kind = scheduled (slice 13 brief, C9). With kind read-only (a hook that a
// project references) the value is text, not a select.
document.addEventListener("DOMContentLoaded", function () {
  var box = document.querySelector("fieldset.scheduled-only");
  if (!box) return;
  var select = document.getElementById("id_kind");
  var readonly = document.querySelector(".field-kind .readonly");
  function current() {
    if (select) return select.value;
    return readonly ? readonly.textContent.trim() : "";
  }
  function sync() { box.style.display = current() === "scheduled" ? "" : "none"; }
  if (select) select.addEventListener("change", sync);
  sync();
});
