# Kontaktformular

Statische Seite mit einem Kontaktformular (`index.html`, `form.js`). Die Prüfregeln stehen in `form.js` und werden
mit dem eingebauten Test-Runner von Node getestet: `npm test` (= `node --test`).

Regeln laut Anforderung:
- Name: Pflichtfeld, mindestens 2 Zeichen ohne führende/abschließende Leerzeichen
- E-Mail: Pflichtfeld, gültige Adresse (Teil vor und nach dem @, Domain mit Punkt)
- Alter: optional; wenn angegeben, ganze Zahl von 16 bis 120
- Nachricht: Pflichtfeld, höchstens 500 Zeichen
