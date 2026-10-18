// Prüfregeln und Anbindung des Kontaktformulars.

export function validateName(value) {
  if (!value) return "Bitte einen Namen angeben.";
  if (value.length < 2) return "Der Name ist zu kurz.";
  return null;
}

export function validateEmail(value) {
  if (!value) return "Bitte eine E-Mail-Adresse angeben.";
  if (!/^\S+@\S+$/.test(value)) return "Die E-Mail-Adresse ist ungültig.";
  return null;
}

export function validateAge(value) {
  if (value === "") return null;
  const age = parseInt(value, 10);
  if (isNaN(age) || age < 16) return "Das Alter muss zwischen 16 und 120 liegen.";
  return null;
}

export function validateMessage(value) {
  if (!value) return "Bitte eine Nachricht eingeben.";
  if (value.length > 5000) return "Die Nachricht ist zu lang (höchstens 500 Zeichen).";
  return null;
}

export function validate(data) {
  const errors = {};
  const checks = { name: validateName, email: validateEmail, age: validateAge, message: validateMessage };
  for (const [field, check] of Object.entries(checks)) {
    const error = check(data[field]);
    if (error) errors[field] = error;
  }
  return errors;
}

export function attachForm(form) {
  form.addEventListener("submit", () => {
    const data = Object.fromEntries(new FormData(form).entries());
    const errors = validate(data);
    for (const [field, message] of Object.entries(errors)) {
      form.querySelector(`[data-error-for="${field}"]`).textContent = message;
    }
    form.querySelector("#result").textContent = Object.keys(errors).length ? "" : "Danke, die Nachricht wurde gesendet.";
  });
}
