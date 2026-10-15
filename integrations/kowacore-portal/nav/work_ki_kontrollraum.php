<?php
/*
ICON: cpu
TITLE: KI-Kontrollraum
PRIO: 19
BEREICH: work
RECHT: dashboard_anzeigen
*/
// Managed by Hermclaw - Kontrollraum-Button von Hermclaw Next (Ansible-Rolle hermclaw_ui, D-013).
// Wird bei jedem Deployment neu geschrieben; Quelle integrations/kowacore-portal im Hermclaw-Repository.
?>
<section class="seitenkopf">
    <div>
        <p class="seitenkopf__eyebrow">Arbeit</p>
        <h1>KI-Kontrollraum</h1>
        <p>Hermclaw Next: Auftr&auml;ge, Pl&auml;ne, Worker, Ressourcen und Live-Ereignisse des autonomen KI-Workers.</p>
    </div>
    <div class="seitenkopf__aktionen">
        <a class="button button--primaer" href="http://192.168.178.223:8080/" target="_blank" rel="noopener noreferrer">KI-Kontrollraum &ouml;ffnen</a>
    </div>
</section>
<section class="panel">
    <header class="panel__kopf">
        <div>
            <h2>Hinweise</h2>
            <p>Der Kontrollraum &ouml;ffnet sich in einem neuen Tab und ist nur im Heimnetz erreichbar.</p>
        </div>
        <span class="badge badge--information">Heimnetz</span>
    </header>
    <p>Anmeldung mit einem pers&ouml;nlichen Hermclaw-Zugangstoken, nicht mit dem Portal-Login. Das Token gilt nur im jeweiligen Tab.</p>
    <p>Adresse: <code>http://192.168.178.223:8080/</code></p>
</section>
