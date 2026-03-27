---
## Korte gebruikersdocumentatie: if/else in workflows

### Wat is het?
Met de nieuwe `type: if` step kun je meerdere stappen groeperen onder een enkele voorwaarde, met een duidelijke then/else-structuur. Dit maakt je workflow overzichtelijker en makkelijker te onderhouden.

### Hoe gebruik je het?
```yaml
steps:
  - id: if_bob
    type: if
    condition:
      context_key: person
      equals: Bob
    then:
      - id: true_step
        type: liquid
        template: "TRUE voor {{ person }}"
    else:
      - id: false_step
        type: liquid
        template: "FALSE voor {{ person }}"
```

### Tips
- Je kunt if/else-steps nesten voor complexe logica.
- `else` is optioneel; alleen `then` mag ook.
- Lege then/else arrays zijn toegestaan.
- Je kunt if/else combineren met for_each, repeat_until, etc.

### Migratie van per-step when
Voorheen moest je per stap een `when`-conditie opgeven. Met if/else groepeer je stappen en hoef je de conditie maar één keer te schrijven.

**Voorbeeld oud:**
```yaml
steps:
  - id: step1
    type: liquid
    when:
      context_key: person
      equals: Bob
    template: "TRUE voor {{ person }}"
  - id: step2
    type: liquid
    when:
      context_key: person
      equals: Alice
    template: "FALSE voor {{ person }}"
```

**Voorbeeld nieuw:**
```yaml
steps:
  - id: if_bob
    type: if
    condition:
      context_key: person
      equals: Bob
    then:
      - id: true_step
        type: liquid
        template: "TRUE voor {{ person }}"
    else:
      - id: false_step
        type: liquid
        template: "FALSE voor {{ person }}"
```

### Meer weten?
Zie de testvoorbeelden hierboven voor meer scenario’s.
---
---
## Dashboard rendering/visualisatie voorstel

### Doel
Maak splitsingen in de workflow visueel duidelijk bij een if-step, met herkenbare takken voor then/else en ondersteuning voor nesting.

### Voorstel
- Herken `type: if` in de visualisatielaag.
- Teken een split-node met twee uitgaande lijnen: één voor then, één voor else.
- Label de lijnen/takken met “then” en “else”.
- Render de substeps van then/else als normale steps, maar visueel onder de juiste tak.
- Nested if’s: render als nieuwe splits binnen de tak.
- Toon de condition (bijv. “person == Bob”) als label bij de split.
- Toon lege then/else als lege tak (of verberg als UX dat vereist).

### UX-voorbeeld (tekstueel)

```
┌────────────┐
│ if: person == Bob
└─────┬──────┘
      │
   ┌──┴─────┐
   │        │
 then     else
  │         │
 step1    step2
  │         │
 nested   ...
```

### Edge cases
- Alleen then: alleen één tak tonen.
- Lege then/else: tak tonen als leeg of verbergen.
- Nested if: splits binnen splits.

### Aanpak
- UI-component voor if-split maken (herbruikbaar).
- Recursief renderen van substeps.
- Condition als label tonen.

---
---
## Testvoorbeelden en edge cases

### 1. Simpele if/else
```yaml
steps:
  - id: if_bob
    type: if
    condition:
      context_key: person
      equals: Bob
    then:
      - id: true_step
        type: liquid
        template: "TRUE voor {{ person }}"
    else:
      - id: false_step
        type: liquid
        template: "FALSE voor {{ person }}"
```

### 2. Nested if
```yaml
steps:
  - id: if_bob
    type: if
    condition:
      context_key: person
      equals: Bob
    then:
      - id: nested_if
        type: if
        condition:
          context_key: age
          equals: 42
        then:
          - id: nested_true
            type: liquid
            template: "Bob is 42!"
        else:
          - id: nested_false
            type: liquid
            template: "Bob is niet 42."
    else:
      - id: not_bob
        type: liquid
        template: "Niet Bob."
```

### 3. Lege then/else
```yaml
steps:
  - id: if_bob
    type: if
    condition:
      context_key: person
      equals: Bob
    then: []
    else:
      - id: not_bob
        type: liquid
        template: "Niet Bob."
```

### 4. Alleen then (geen else)
```yaml
steps:
  - id: if_bob
    type: if
    condition:
      context_key: person
      equals: Bob
    then:
      - id: true_step
        type: liquid
        template: "TRUE voor {{ person }}"
```

### 5. For_each met if
```yaml
steps:
  - id: loop_persons
    type: for_each
    items_from: persons
    steps:
      - id: if_bob
        type: if
        condition:
          context_key: person
          equals: Bob
        then:
          - id: true_step
            type: liquid
            template: "TRUE voor {{ person }}"
        else:
          - id: false_step
            type: liquid
            template: "FALSE voor {{ person }}"
```
---
---
## Engine-aanpassing: parser & executor (pseudocode)

### Parsing
- Herken `type: if` als geldig step type.
- Valideer presence van `condition` en `then` (en optioneel `else`).
- Parseer substeps in `then` en `else` als gewone steps (recursief).

### Executor (voorbeeld in Python-achtige pseudocode)
```python
def execute_steps(steps, context):
  for step in steps:
    if step['type'] == 'if':
      cond = step['condition']
      # Eenvoudige evaluatie, uitbreidbaar
      value = context.get(cond['context_key'])
      if value == cond['equals']:
        execute_steps(step.get('then', []), context)
      else:
        execute_steps(step.get('else', []), context)
    else:
      execute_step(step, context)

def execute_step(step, context):
  # Bestaande logica voor liquid/http/etc
  pass
```

### Uitleg
- `execute_steps` is recursief: substeps in then/else kunnen zelf ook if zijn.
- Condition evaluatie kan later uitgebreid worden (not, and, or, etc).
- Als then/else ontbreekt of leeg is: gewoon overslaan.

### Foutafhandeling
- Valideer bij parsing: condition en then verplicht, else optioneel.
- Bij ongeldige structuur: duidelijke foutmelding.

---
## Feature: Native IF/ELSE branching in MiniCloud workflows

### Doel
Ondersteun een echte `if`-step in workflows, zodat meerdere stappen per branch (then/else) mogelijk zijn, met duidelijke YAML-structuur en visuele splitsing in de UI.

### Requirements
- Een step met `type: if` kan overal in de steps-array staan (ook genest).
- Een `if`-step bevat:
  - `id`: unieke step-id
  - `type: if`
  - `condition`: object met logische expressie (minimaal: context_key, equals)
  - `then`: array van steps (uitgevoerd als condition waar is)
  - `else`: array van steps (optioneel, uitgevoerd als condition niet waar is)
- Substeps in then/else zijn gewone steps (kunnen zelf ook if zijn).
- Condition kan later uitgebreid worden (not, and, or, etc).
- Backward compatible: bestaande per-step `when` blijft werken.

### YAML-syntax voorbeeld
```yaml
steps:
  - id: if_bob
    type: if
    condition:
      context_key: person
      equals: Bob
    then:
      - id: congrats_bob
        type: liquid
        input_from: initial
        template: "Gefeliciteerd, {{ person }}!"
      - id: extra_true
        type: liquid
        input_from: initial
        template: "Extra TRUE-stap voor {{ person }}."
    else:
      - id: congrats_other
        type: liquid
        input_from: initial
        template: "Gefeliciteerd, {{ person }}!"
      - id: extra_false
        type: liquid
        input_from: initial
        template: "Extra FALSE-stap voor {{ person }}."
```

### Use cases
- Meerdere stappen per branch zonder herhaling van condities.
- Geneste if’s voor complexe logica.
- Duidelijke splitsing in dashboard/visualisatie.

### Edge cases
- Lege then/else (mag, wordt overgeslagen).
- Alleen then (else optioneel).
- Nested if’s in then/else.

### Volgende stappen
- Engine: parser & executor uitbreiden.
- Dashboard: splits/branches visueel tonen.
- Testen: unit tests, demo-workflows.