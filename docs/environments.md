# Omgevingen (DTAP) & Lokaal Testen in Minikube

MiniCloud maakt gebruik van het Kubernetes principe van **volledig gescheiden omgevingen** (Namespaces) gecombineerd met Kustomize overlays. Dit betekent dat je per omgeving (DEV, TST, ACC, PRD) een eigen geïsoleerde installatie draait, inclusief een eigen Dashboard en Orchestrator.

## 1. Doel & Werking

De Docker-containers (minicloud/orchestrator, minicloud/dashboard, etc.) zijn qua image **exact hetzelfde** (ze bevatten dezelfde broncode). Wat de omgevingen uniek maakt, wordt ingespoten (geïnjecteerd) via Kustomize:
- **Namespaces:** Isolatie van resources (bijv. \minicloud-dev\ of \minicloud-prd\).
- **Configuratie (Env Vars):** Zo kan in DEV de \STORAGE_ACL_READ_ROLES\ op \orchestrator,developer\ staan, en op PRD veel strenger zijn.
- **Domeinnamen (Ingress):** Elke omgeving luistert naar een eigen (sub)domein.
- **Versies (Tags):** DEV kan de :dev tag draaien (vaak de laatste commit), PRD draait :latest of een specifieke release tag.

## 2. Omgevingen Uitrollen

In de map \deploy/k8s/overlays/\ vind je de kant-en-klare Kustomize profielen: \dev\, \	st\, \cc\ en \prd\.

### Optie A: Handmatig toepassen (via Kustomize)
Als je via je terminal toegang hebt tot je cluster (of Minikube), dan roep je simpelweg de specifieke map aan:
\\\ash
kubectl apply -k deploy/k8s/overlays/dev
kubectl apply -k deploy/k8s/overlays/tst
kubectl apply -k deploy/k8s/overlays/acc
kubectl apply -k deploy/k8s/overlays/prd
\\\

### Optie B: Automatisch (via CI/CD scripts)
Wil je dit automatiseren? Gebruik dan het meegeleverde script \deploy/k8s/scripts/gitlab-deploy.sh\. 
Kopieer hiervoor \deploy-config.example.env\ naar \deploy-config.local.env\, pas hierin in elk geval de variabele \NAMESPACE\ (en registry) aan en voer het script uit. Dit wordt normaal gesproken gedaan in je pipelines (GitLab CI/CD).

## 3. Van Omgeving Wisselen (Het Dashboard)

Er is **geen dropdown** of wissel-knop in het MiniCloud Dashboard. Omdat iedere omgeving in totale isolatie draait, is wissel simpelweg een kwestie van **naar de specifieke URL van die omgeving gaan**:
- DEV: \http(s)://minicloud-dev.jouw-domein.nl\
- TST: \http(s)://minicloud-tst.jouw-domein.nl\
- ACC: \http(s)://minicloud-acc.jouw-domein.nl\
- PRD: \http(s)://minicloud.jouw-domein.nl\

*(In de Ingress-patch van het betreffende yaml bestand in \deploy/k8s/overlays/<omgeving>/kustomization.yaml\ stel je de specifieke hostname in).*

## 4. Lokaal Testen in Minikube (Host-Name Faken)

Het is enorm handig om dit mechanisme lokaal te testen. Je kunt Minikube gebruiken en via je locale \hosts\ bestand net doen alsof die echte domeinen bestaan.

**Stap 1: Ingress over Minikube activeren**
Zorg dat je Ingress addon aan staat en bereikbaar is in Minikube. Zet minikube tunnel aan of port-forward poort 80/443.

**Stap 2: Overlay applyen**
Pas bijvoorbeeld de DEV-omgeving toe:
\\\ash
kubectl apply -k deploy/k8s/overlays/dev
\\\

**Stap 3: Het IP opzoeken**
Vraag het IP-adres van je Minikube-cluster op:
\\\ash
minikube ip
# Bijvoorbeeld: 192.168.49.2
\\\

**Stap 4: Je Hosts-bestand aanpassen (op Windows)**
Open een teksteditor (Kladblok) als *Administrator* en open het bestand:
\C:\Windows\System32\drivers\etc\hosts\

Voeg helemaal onderaan een nieuwe regel toe met je Minikube IP en de domeinen (die in de kustomization.yaml staan ingesteld):
\\\	ext
192.168.49.2 minicloud-dev.example.com
192.168.49.2 minicloud-tst.example.com
\\\

Sla het bestand op. Pingt je pc nu dit adres, dan wordt het onzichtbaar doorgestuurd naar je lokale Minikube cluster.

**Stap 5: Resultaat**
Je kunt nu in je normale webbrowser gaan naar:
**\http://minicloud-dev.example.com\**

De Kubernetes Ingress-controller ontvangt het verzoek, leest in de host-header waar je naartoe wilde en stuurt je (isolatie gewaarborgd) feilloos naar de *Gateways/Dashboards* die zich ín de betreffende Namespace bevinden!
