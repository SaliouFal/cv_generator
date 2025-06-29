###################################################################################################################""""

# ─────────────────────────────────────────────────────────────────────────────
# app.py  –  version « split 30 s » (Heroku friendly)
# ─────────────────────────────────────────────────────────────────────────────
import os, json, uuid, shutil, subprocess, re, textwrap
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, send_file, flash, jsonify
)
import docx
import openai

# ───────────────────── Configuration ──────────────────────
openai.api_key = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = "o3"

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.config.update(
    SECRET_KEY    = os.getenv("FLASK_SECRET", "change_me"),
    UPLOAD_FOLDER = BASE_DIR / "static" / "uploads",
    OUTPUT_FOLDER = BASE_DIR / "generated",
)

with open(BASE_DIR / "config.json", encoding="utf-8") as fh:
    CV_TEMPLATES = json.load(fh)

def get_template_meta(tid):                                      # utilitaire
    return next((t for t in CV_TEMPLATES if t["id"] == tid), None)

# ───────────────────── Mini-formulaire ─────────────────────
# ────────── parsing des tableaux jobs[] / degrees[] ──────────
array_re = re.compile(r"(?P<fld>jobs|degrees)\[(?P<idx>\d+)]\[(?P<key>\w+)]")


# ───────────────────── GPT helpers ─────────────────────────
# ─────────── GPT : auto-complétion du JSON de CV ─────────────────────
def gpt_autofill(schema: dict, partial: dict) -> dict:
    """
    Demande à GPT de compléter le JSON du CV.
    On force le retour au format JSON strict grâce à response_format.
    """
    sys = (
        "Tu es un générateur de CV. Complète l'objet JSON pour qu’il respecte "
        "le schema fourni. N’altère PAS les champs déjà présents "
        "(nom, dates, etc.). Réponds uniquement par un JSON object."
    )

    user = (
        "POSTE_CIBLE:\n" + partial.get("target_role", "") +
        "\n\nPARTIEL:\n" + json.dumps(partial, ensure_ascii=False, indent=2) +
        "\n\nSCHEMA:\n"  + json.dumps(schema,  ensure_ascii=False, indent=2)
    )

    try:
        resp = openai.ChatCompletion.create(
            model           = OPENAI_MODEL,
            response_format = {"type": "json_object"},
            messages        = [
                {"role": "system", "content": sys},
                {"role": "user",   "content": user}
            ]
        )
        
        return json.loads(resp.choices[0].message.content)
   # ← renvoie déjà un dict
                                                #   (pas besoin de json.loads)
    except openai.OpenAIError as e:
        # On journalise et relaie l’erreur claire au front-end
        app.logger.error("OpenAI JSON error: %s", e)
        raise RuntimeError("GPT n’a pas pu générer un JSON valide : " + str(e))

# ───────────────────── Compilation PDF ─────────────────────
def copy_template(meta):
    src = Path(meta["latex_path"]).parent
    dst = app.config["OUTPUT_FOLDER"]/uuid.uuid4().hex
    shutil.copytree(src, dst); return dst



# ---------------------------------------------------------------------------
# Correctif : compile_pdf – tolérance warnings LaTeX
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Compilation PDF : pdflatex → fallback xelatex
# ---------------------------------------------------------------------------
# def compile_pdf(tex: str, meta: dict, ctx: dict) -> Path:
#     """
#     Compile *tex* en PDF.

#     1. Tente d’abord **pdflatex**.
#     2. Si le PDF n’est pas produit ou fait < 1 Ko, retente avec **xelatex**.
#     3. Lève RuntimeError si les deux moteurs échouent.

#     Tolère les codes de sortie ≠ 0 lorsqu’un PDF valide est généré
#     (LaTeX émet souvent des warnings).
#     """
#     build = copy_template(meta)
#     tex_path = build / "main.tex"
#     tex_path.write_text(tex, encoding="utf-8")

#     # ── copie éventuelle de la photo ────────────────────────────────────────
#     if (photo := ctx.get("photo")):
#         up = app.config["UPLOAD_FOLDER"] / photo
#         if up.is_file():
#             shutil.copy(up, build / photo)

#     def _run(engine: str) -> tuple[Path, str, int]:
#         """Exécute le moteur LaTeX et renvoie (pdf, log, code)."""
#         proc = subprocess.run(
#             [engine, "-interaction=nonstopmode", tex_path.name],
#             cwd=build,
#             stdout=subprocess.PIPE,
#             stderr=subprocess.STDOUT,
#             text=True,
#         )
#         return build / "main.pdf", proc.stdout, proc.returncode

#     # 2️⃣  second essai : xelatex
#     pdf, log, code = _run("xelatex")
#     if pdf.exists() and pdf.stat().st_size > 1024:
#         if code != 0:
#             app.logger.warning("xelatex terminé avec code %s (warnings)", code)
#         return pdf

#     # 3️⃣  échec total
#     raise RuntimeError(
#         "La compilation a échoué avec pdflatex puis xelatex.\n" + log[-1500:]
#     )


import base64, requests, tempfile
from pathlib import Path

LATEX_API = "https://latex.ytotech.com/builds/sync"   # endpoint « synchrone »

def compile_pdf_remote(tex: str,
                       workdir: Path,
                       extra_files: dict[str, Path] | None = None,
                       compiler: str = "xelatex") -> Path:
    """
    Envoie *tex* (et les fichiers annexes) au service LaTeX-on-HTTP
    et renvoie le chemin local du PDF généré.
    - workdir : dossier de travail (déjà créé) où l’on veut stocker le PDF.
    - extra_files : {"logo.png": Path("local/logo.png"), …}
    """
    # -- 1. prépare la liste des « resources » -----------------------------
    resources = [{
        "main": True,
        "content": tex                     # directement la chaîne LaTeX
    }]

    for relpath, filepath in (extra_files or {}).items():
        b64 = base64.b64encode(filepath.read_bytes()).decode()
        resources.append({"path": relpath, "file": b64})

    payload = {"compiler": compiler, "resources": resources}

    resp = requests.post(LATEX_API, json=payload, timeout=90)

    # on accepte 200 ou 201  (+ autres 2xx éventuels)
    ok_status = 200 <= resp.status_code < 300
    pdf_ct    = resp.headers.get("content-type", "").startswith("application/pdf")
    if not (ok_status and pdf_ct):
        # derniers 800 caractères s’il y a des logs JSON
        raise RuntimeError(f"LaTeX API HTTP {resp.status_code}: {resp.text[:800]}")

    out_pdf = workdir / "main.pdf"
    out_pdf.write_bytes(resp.content)
    return out_pdf


def gpt_render(template_tex:str, data:dict)->str:
    """Injecte les données + améliore la mise en page, renvoie le LaTeX final."""
    sys = (
    # — rôle et langue -----------------------------------------------------------------
    "Tu es un expert LaTeX francophone. On te fournit 1) un template LaTeX complet "
    "et 2) les données brutes d’un formulaire de CV.\n"
    # — règle d’or ----------------------------------------------------------------------
    "⚠️  Règle ABSOLUE : tu n’inventes JAMAIS de contenu ni de section. "
    "Tu n’utilises QUE les clés et valeurs qui existent réellement dans l’objet "
    "données. Si une clé, un tableau ou une valeur n’est pas présent, tu n’ajoutes "
    "rien, tu laisses la section vide ou tu supprimes entièrement le bloc "
    "correspondant du template.\n"
    # — sections à ne pas créer ---------------------------------------------------------
    "Ne crée donc pas (même vide) : Langues, Compétences, Centres d’intérêt, "
    "Certifications, Descriptions de formation, Descriptions d’expérience et Projet de formation etc."
    "s’ils ne sont pas déjà fournis.\n"
    # — description : reformulation limitée --------------------------------------------
    "Si un champ « description » existe déjà dans jobs/degrees, reformule-le de maniere un peu plus developpé en"
    "bullet points clairs (verbe d’action, résultats). "
    "S’il est vide ou inexistant, n’ajoute rien.\n"
    # — résumé professionnel ------------------------------------------------------------
    "Tu peux SEULEMENT rédiger un résumé (summary) **si le champ summary est présent "
    "et fait moins de 20 mots**. Sinon, garde-le tel quel.\n"
    # — rendu LaTeX --------------------------------------------------------------------
    "Remplace les variables du template par les valeurs fournies, améliore la "
    "lisibilité LaTeX (sauts de ligne, \\item) et insère le champ photo tel quel "
    "dans \\includegraphics.\n"
    # — sortie -------------------------------------------------------------------------
    "Réponds STRICTEMENT avec le code LaTeX final, sans ``` ni explications."
)
    

    user = ("TEMPLATE:\n" + template_tex +
            "\n\nDONNÉES:\n" + json.dumps(data, ensure_ascii=False, indent=2))
    out  = openai.ChatCompletion.create(model=OPENAI_MODEL,
             messages=[{"role":"system","content":sys},{"role":"user","content":user}]
           ).choices[0].message.content
    
    return re.sub(r"^```.*?\\n|\\n?```$", "", out, flags=re.S).strip()


def _first(val):
    """Renvoie la 1ʳᵉ valeur si c’est une liste, sinon la valeur elle-même."""
    return val[0] if isinstance(val, list) else val

def extract_arrays(form) -> dict:
    """
    Transforme jobs[0][title] → {'jobs':[...], 'degrees':[...]} en conservant l’ordre.
    Accepte aussi bien ImmutableMultiDict que dict simple.
    """
    buf = {}

    # si c’est un ImmutableMultiDict on veut (.items()) → (clé, valeur unique)
    # si c’est un dict classique issu de to_dict(flat=False) la valeur est liste
    for k, v in (form.items() if hasattr(form, "items") else form):
        m = array_re.fullmatch(k)
        if not m:
            continue
        fld, idx, key = m.group("fld"), int(m.group("idx")), m.group("key")
        buf.setdefault((fld, idx), {})[key] = _first(v).strip()

    out = {"jobs": [], "degrees": [], "languages": []}
    for (fld, idx), row in sorted(buf.items(), key=lambda p: p[0][1]):
        out.setdefault(fld, []).append(row)
    return out

# ════════════════════════════════════════════════════════════════════════════
#  1.  GPT / PDF helpers  (inchangés)
# ════════════════════════════════════════════════════════════════════════════
# … (gardez inchangé tout le bloc de fonctions : gpt_extract_from_cv,
#    gpt_autofill, gpt_render, copy_template, compile_pdf,
#    extract_arrays, gpt_edit) …


# ════════════════════════════════════════════════════════════════════════════
#  2.  ROUTES
# ════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html", templates=CV_TEMPLATES)


# --------- Choix du template + méthode d’entrée -----------------------------
@app.route("/select/<template_id>")
def select_template(template_id):
    if not get_template_meta(template_id):
        flash("Modèle invalide", "danger")
        return redirect(url_for("index"))
    session.clear()
    session["template_id"] = template_id
    return redirect(url_for("choose_input"))          # page qui propose « formulaire » ou « import »


@app.route("/choose_input")
def choose_input():
    if "template_id" not in session:
        return redirect(url_for("index"))
    return render_template("choose_input.html")



# ---------------------------------------------------------------
# 0. utilitaires remplacement LaTeX
# ---------------------------------------------------------------
import re
# ------------------------------------------------------------------
# helpers remplacement
# ------------------------------------------------------------------
PLACEHOLDER_PAT = re.compile(r"%%[^%]+%%")

def _first(val):                     # ⇒ str
    return val[0] if isinstance(val, list) else val

def _as_list(form, prefix):
    """Renvoie toutes les valeurs dont la clé commence par prefix."""
    return [v for k, v in form.items() if k.startswith(prefix)]

CERT_RE = re.compile(r"certifications\[(\d+)]\[(title|issuer|date)]")
def build_placeholders(form: dict, photo_name:str="") -> dict:
    """
    Construit un mapping {%%placeholder%%: valeur_str}.
    """
    ph = {}
    print(form)
    # --- champs simples -------------------------------------------------
    simples = ["first_name", "last_name", "headline",
               "phone", "linkedin", "address", "website", "summary"]
    for k in simples:
        ph[f"%%{k}%%"] = _first(form.get(k, ""))

    # --- photo ----------------------------------------------------------
    ph["%%photo%%"] = photo_name                     # balise LaTeX
    # (on renverra aussi le vrai nom pour compile_pdf)

    # --- compétences ----------------------------------------------------
    # tes champs sont skills[0], skills[1]…
    for k, v in form.items():
        m = re.fullmatch(r"skills\[(\d+)]", k)
        if m:
            ph[f"%%skills[{m.group(1)}]%%"] = _first(v)

    # --- certifications -------------------------------------------------
    
    # cert_rows = {}
    # for k, v in form.items():
    #     m = CERT_RE.fullmatch(k)
    #     if m:
    #         idx, field = int(m.group(1)), m.group(2)
    #         cert_rows.setdefault(idx, {})[field] = _first(v)

    # for i, row in cert_rows.items():
    #     # On crée UNE seule chaîne par certif  ➜  "Titre — Organisme (Date)"
    #     txt = row.get("title", "")
    #     if issuer := row.get("issuer"): txt += f" — {issuer}"
    #     if date   := row.get("date"):   txt += f" ({date})"
    #     ph[f"%%certifications[{i}]%%"] = txt
    # --- expériences & diplômes ----------------------------------------
    arrs = extract_arrays(form)      # -> {'jobs':[…], 'degrees':[…]}

    #  A) JOBS  ->  experiences[…]
    for i, row in enumerate(arrs["jobs"]):
        ph[f"%%experiences[{i}].company%%"]     = row.get("company", "")
        ph[f"%%experiences[{i}].title%%"]       = row.get("title",   "")
        ph[f"%%experiences[{i}].start%%"]       = row.get("dates","").split("–")[0]
        ph[f"%%experiences[{i}].end%%"]         = row.get("dates","").split("–")[-1]
        ph[f"%%experiences[{i}].description[0]%%"] = row.get("description","")

    #  B) DEGREES
    for i, row in enumerate(arrs["degrees"]):
        ph[f"%%degrees[{i}].title%%"]       = row.get("degree","")
        ph[f"%%degrees[{i}].school%%"]      = row.get("institution","")
        ph[f"%%degrees[{i}].year%%"]        = row.get("dates","")
        ph[f"%%degrees[{i}].subject%%"]     = row.get("description","")

    return ph
#PLACEHOLDER_PAT = re.compile(r"%%[^%]+%%")     # déjà défini ailleurs

def _as_str(val):
    """Garantie une string ; liste → join, autre → str()."""
    if isinstance(val, list):
        return ", ".join(map(str, val))
    return str(val)

LATEX_SPECIALS = {
    '\\': r'\textbackslash{}',
    '{':  r'\{',  '}': r'\}',
    '$':  r'\$',
    '&':  r'\&',
    '%':  r'\%',
    '#':  r'\#',
    '_':  r'\_',
    '^':  r'\^{}',
    '~':  r'\~{}',
}

def latex_escape(text: str) -> str:
    for c, repl in LATEX_SPECIALS.items():
        text = text.replace(c, repl)
    return text



def apply_placeholders(template_tex: str, ph: dict) -> str:
    tex = template_tex
    for key, val in ph.items():
        tex = tex.replace(key, latex_escape(str(val)))
    return PLACEHOLDER_PAT.sub("", tex)



# ───────── helper : extraction GPT depuis un CV PDF/Word ─────────
def gpt_extract_from_cv(schema: dict, plain_text: str) -> dict:
    """
    Reçoit le texte intégral d’un CV (déjà OCR ou converti en texte brut)
    et renvoie un JSON conforme au schema, complétant tous les champs.
    """
    sys = (
      "Tu es un parser de CV. On te donne le texte brut d’un CV existant "
      "et un schema JSON cible. "
      "Récupère toutes les informations présentes ; "
      "si un champ est manquant dans le texte, laisse-le vide. "
      "Ne crée pas d’expérience ou de diplôme qui n’existe pas. "
      "Réponds STRICTEMENT avec le JSON conforme au schema."
    )
    user = (
      "SCHEMA :\n" + json.dumps(schema, ensure_ascii=False, indent=2) +
      "\n\nCV EN TEXTE BRUT :\n" + plain_text[:6000]  # tranche pour rester court
    )
    out = openai.ChatCompletion.create(
        model=OPENAI_MODEL, 
        messages=[{"role":"system","content":sys},{"role":"user","content":user}]
    ).choices[0].message.content
    return json.loads(re.sub(r"^```json|```$","",out,flags=re.I).strip())









# @app.route("/import_cv", methods=["POST"])
# def import_cv():
#     if "template_id" not in session:
#         return redirect(url_for("index"))

#     up = request.files.get("cvfile")
#     if not up or not up.filename:
#         flash("Choisissez un fichier !", "warning")
#         return redirect(url_for("choose_input"))
    

#     # --- 2. photo optionnelle -------------------------------------------
#     photo = request.files.get("photo")
#     if photo and photo.filename:
#         photo_name = f"{uuid.uuid4().hex}{Path(photo.filename).suffix}"
#         dest = app.config["UPLOAD_FOLDER"] / photo_name
#         dest.parent.mkdir(parents=True, exist_ok=True)
#         photo.save(dest)
#         session["photo_file"] = photo_name
#     else:
#         session["photo_file"] = ""




#     ext = Path(up.filename).suffix.lower()
#     fname = f"{uuid.uuid4().hex}{ext}"
#     temp  = app.config["UPLOAD_FOLDER"]/fname
#     temp.parent.mkdir(parents=True, exist_ok=True)
#     up.save(temp)

#     # --- 1. convertir en texte brut ----------------------------------------
#     if ext == ".pdf":
#         # nécessite 'pdftotext' installé (poppler) ou pdfminer
#         txt = subprocess.check_output(["pdftotext", "-layout", str(temp), "-"]).decode("utf-8", errors="ignore")
#     elif ext in (".docx", ".doc"):
#         # simple extraction avec python-docx (docx) ou mammoth; ici pseudo-code
        
#         doc  = docx.Document(str(temp))
#         txt  = "\n".join(p.text for p in doc.paragraphs)
#     else:
#         flash("Format non pris en charge (PDF ou DOCX)", "danger")
#         return redirect(url_for("choose_input"))

#     # --- 2. appeler GPT pour mapper le texte → JSON complet -----------------
#     meta   = get_template_meta(session["template_id"])
#     schema = json.loads(Path(meta["latex_path"]).with_name("schema.json").read_text())
#     session["cv_data"] = gpt_extract_from_cv(schema, txt)

#     return redirect(url_for("preview"))

# ─── utils_latex.py  (ou en haut de app.py) ───────────────────────
import re
LATEX_RE = re.compile(r'\\documentclass[\s\S]*?\\end{document}', re.I)

def clean_latex(raw: str) -> str:
    """
    Extrait le bloc LaTeX complet et jette tout le reste.
    Lève RuntimeError si aucun \documentclass … \end{document} n’est trouvé.
    """
    raw = raw.strip().lstrip("```").rstrip("```")        # premier filtre rapide
    m = LATEX_RE.search(raw)
    if not m:
        raise RuntimeError("GPT n’a pas renvoyé de code LaTeX valide")
    return m.group(0).strip()






def gpt_fill_plain(template_tex: str, plain_text: str, photo_name: str = "", rules: str = "") -> str:
    """Remplit *template_tex* avec *plain_text* (CV brut) + insère *photo_name*.
    *photo_name* est le nom du fichier copié dans le dossier build, à utiliser
    tel quel dans \includegraphics{…}."""

    photo_instr = (
        f"Le fichier photo à utiliser est : {photo_name}. "
        "Si le template contient déjà une commande \\includegraphics, remplace son argument par ce nom. "
        "Sinon, insère cette commande à l’endroit approprié (en haut du CV)."
        if photo_name else ""
    )

    # sys = (
    #     "Tu es un expert LaTeX francophone. On te fournit :\\n"
    #     "1) un template LaTeX complet ;\\n"
    #     "2) le texte brut d’un CV existant.\\n"
    #     "N’utilise jamais le caractère “&” ; écris “et” ou échappe-le \&."
    #     "Ta mission : extraire les informations et les placer dans le template et le cv doit etre le modèle que le template fourni. Tu dois garder les memes couleurs et en gros le modèle tout entier.\\n"
    #     "⚠️  N’invente JAMAIS de contenu (noms, dates, chiffres).\\n"
    #     "Reformule toutes les descriptions d’expériences afin qu’elles deviennent "
    #     "2–3 phrases fluides (ou bullet points) en réutilisant **uniquement** les "
    #     "éléments présents.\\n"
    #     "Pour chaque formation sans description, rédige UNE phrase générique "
    #     "décrivant brièvement la spécialisation ou les compétences liées, "
    #     "d’après le titre.\\n"
    #     "Ne crée pas Langues, Compétences, Centres d’intérêt, Certifications, etc. "
    #     "s’ils n’existent pas.\\n"
    #     "• Pour chaque entrée de “Compétences”  formate les libellés"
    #      "sur **une longueur maximale de 20 caractères** ; si nécessaire,"
    #     "coupe la phrase logiquement et ajoute \\ pour forcer le retour à la ligne.\\n"
    #     "• Pour chaque entrée de “Compétences”  formate les libellés"
    #     "sur **une longueur maximale de 45 caractères** ; si nécessaire,"
    #     "coupe la phrase logiquement et ajoute \\ pour forcer le retour à la ligne.\\n"
    #     "• Pour chaque formation "
    #     "– Si un champ description existe, reformule-le en 2-3 phrases"
    #     "plus fluides : commencer par le diplôme, préciser discipline,"
    #     "méthodes ou logiciels étudiés."   
    #     "– S’il est vide, génère une courte description (2 phrases max)"
    #     "en t’appuyant sur tes connaissances générales du diplôme"
    #     "et des compétences habituellement acquises, sans inventer de"
    #     "dates ni d’établissement.\\n"
    #     "Si le résumé (summary) est absent, crée un résumé de 3–4 phrases fluides en t’appuyant sur les informations du CV (poste cible, années d’expérience, compétences majeures) ; s’il existe déjà, reformule-le en 3–4 phrases plus naturelles.\n"
    #     "sinon, laisse le champ vide.\\n"

    #     + photo_instr + "\\n" + rules + "\\n"

    # )
    
    # NOUVEAU PROMPT OPTIMISÉ
    sys = f"""
Tu es un expert en composition de documents LaTeX, spécialisé dans la transformation de CV. Ta mission est de convertir un CV en texte brut en un document LaTeX impeccable en utilisant un template fourni.

### MISSION PRINCIPALE
Intègre méticuleusement les informations du CV texte dans le template LaTeX. Tu dois respecter à 100% la structure, les couleurs et le style du template.

### RÈGLES D'OR (À APPLIQUER SANS EXCEPTION)
1.  **AUCUNE INVENTION** : N'invente JAMAIS de contenu. Si une information n'est pas présente dans le texte source (noms, dates, chiffres, descriptions), ne l'ajoute pas, sauf indication contraire ci-dessous.
2.  **RESPECT DU CONTENU** : Utilise **uniquement** les mots et les concepts présents dans le CV source pour reformuler les descriptions.
3.  **CARACTÈRE '&'** : N'utilise jamais le caractère `&`. Remplace-le systématiquement par `et` ou, si le contexte LaTeX l'exige, par `\&`.
4.  **SECTIONS** : Ne crée une section (Langues, Centres d’intérêt, etc.) que si elle existe explicitement dans le CV source.

---

### INSTRUCTIONS DÉTAILLÉES PAR SECTION

#### 1. Résumé (Profil)
- **Si un résumé existe** dans le texte source : reformule-le pour obtenir 3 à 4 phrases fluides et percutantes, en conservant le sens original.
- **Si le résumé est absent** : crée-en un de 3 à 4 phrases en te basant sur les informations clés du CV (le poste le plus récent ou le poste cible, les années d'expérience globales, et 2-3 compétences majeures).

#### 2. Expériences Professionnelles
- Pour chaque expérience, reformule la description en 2 ou 3 phrases complètes ou en une liste à puces (`itemize`). La reformulation doit être plus naturelle et professionnelle, mais sans ajouter d'informations qui ne sont pas dans le texte d'origine.

#### 3. Formations
- **Si une description existe** : reformule-la en 2 ou 3 phrases fluides. Commence par le type de diplôme, puis la spécialisation, et enfin les compétences ou logiciels principaux étudiés.
- **Si la description est absente** : rédige une description générique de 1 à 2 phrases maximum. Cette description doit se baser sur le titre du diplôme et les compétences typiquement acquises dans ce cursus.

#### 4. Compétences
- **IMPÉRATIF :** La section des compétences clés doit contenir **exactement 8 compétences**. Ni plus, ni moins. Sélectionne les 8 compétences les plus importantes et pertinentes du CV source pour les inclure.
- **Formatage du libellé** : Chaque libellé de compétence ne doit pas dépasser **45 caractères par ligne**. Si un libellé est plus long, coupe-le à un endroit logique (après un mot) et insère un `\\` pour forcer un retour à la ligne.

---

### 5. ICÔNES AUTORISÉES : 
N’utilise que les pictogrammes **basiques compatibles LaTeX** comme :
`\faUser`, `\faEnvelope`, `\faPhone`, `\faGlobe`, `\faCode`, `\faWrench`, `\faGraduationCap`, `\faBriefcase`.

N’utilise **JAMAIS** d’icônes comme : `\faJsSquare`, `\faPython`, `\faReact`, `\faBrain`, `\faMicrochip`, etc.  
Ces pictogrammes avancés de **FontAwesome Brands** ne sont **pas supportés par le compilateur** et provoquent une erreur.

Si une compétence ne peut pas être associée à une icône standard, **utilise du texte simple** ou une `\item`, sans icône.

### **VÉRIFICATION FINALE**
Avant de terminer, vérifie que :

- Le CV final contient bien 8 compétences.
- Aucun caractère `&` n'est présent.
- Aucune information n'a été inventée.
- Le style du template est parfaitement conservé.
- Le code Latex final marche et que je n'aurais pas d'erreur à la compilation (par exemple qu'il ne presente pas d'accolade pas ferméé et etc .....)

{photo_instr}

{rules}
"""
    user = (
        "TEMPLATE:\n" + template_tex +
        "\n\nCV TEXTE BRUT:\n" + plain_text[:6000]
    )

    out = openai.ChatCompletion.create(
        model       = OPENAI_MODEL,
        messages    = [
            {"role": "system", "content": sys},
            {"role": "user",   "content": user}
        ]
    ).choices[0].message.content
    out=clean_latex(out)
    
    return out

# ---------------------------------------------------------------------------
# Route /import_cv  –  version sans JSON / schema + photo aware
# ---------------------------------------------------------------------------

# @app.route("/import_cv", methods=["POST"])
# def import_cv():
#     """Importe un CV PDF/DOCX (+ photo) → GPT → LaTeX → PDF (photo incluse)."""

#     # 0. vérifie que le template est choisi ---------------------------------
#     if "template_id" not in session:
#         return redirect(url_for("index"))

#     # 1. récup fichiers ------------------------------------------------------
#     cv_file = request.files.get("cvfile")
#     if not cv_file or not cv_file.filename:
#         flash("Choisissez un fichier CV !", "warning")
#         return redirect(url_for("choose_input"))

#     photo_file = request.files.get("photo")

#     ext = Path(cv_file.filename).suffix.lower()
#     if ext not in (".pdf", ".doc", ".docx"):
#         flash("Format non pris en charge (PDF ou DOCX)", "danger")
#         return redirect(url_for("choose_input"))

#     cv_tmp = app.config["UPLOAD_FOLDER"] / f"{uuid.uuid4().hex}{ext}"
#     cv_tmp.parent.mkdir(parents=True, exist_ok=True)
#     cv_file.save(cv_tmp)

#     # 2. photo optionnelle ---------------------------------------------------
#     photo_name = ""
#     if photo_file and photo_file.filename:
#         photo_name = f"{uuid.uuid4().hex}{Path(photo_file.filename).suffix}"
#         dest = app.config["UPLOAD_FOLDER"] / photo_name
#         dest.parent.mkdir(parents=True, exist_ok=True)
#         photo_file.save(dest)
#     session["photo_file"] = photo_name

#     # 3. extraction texte ----------------------------------------------------
#     if ext == ".pdf":
#         plain = subprocess.check_output(["pdftotext", "-layout", str(cv_tmp), "-"]).decode("utf-8", errors="ignore")
#     else:
#         doc = docx.Document(str(cv_tmp))
#         plain = "\n".join(p.text for p in doc.paragraphs)

#     # 4. GPT → LaTeX ---------------------------------------------------------
#     meta         = get_template_meta(session["template_id"])
#     template_tex = Path(meta["latex_path"]).read_text(encoding="utf-8")
#     final_tex    = gpt_fill_plain(template_tex, plain, photo_name)

#     # 5. compilation ---------------------------------------------------------
#     pdf_path = compile_pdf(final_tex, meta, {"photo": photo_name})

#     # 6. session + redirect preview -----------------------------------------
#     session.update({
#         "cv_data":      {},
#         "last_tex":     final_tex,
#         "pdf_filename": pdf_path.relative_to(app.config["OUTPUT_FOLDER"]).as_posix(),
#     })
#     return redirect(url_for("preview"))
###############################################################################################
# Ajouter en haut du fichier
import threading
async_results = {}
# Génère un SID unique pour les sessions
def generate_session_sid():
    return str(uuid.uuid4())
# ... (le reste du code existant) ...
def process_cv_async(cv_path: Path,
                     photo_tmp: str,
                     template_id: str,
                     session_sid: str):
    """Thread de traitement lourd – AUCUN accès à `session` ici !"""
    with app.app_context():
        try:
            # 1) Photo --------------------------------------------------------
            photo_name = ""
            if photo_tmp:
                photo_name = f"{uuid.uuid4().hex}{Path(photo_tmp).suffix}"
                dest = app.config["UPLOAD_FOLDER"] / photo_name
                shutil.move(photo_tmp, dest)

            # 2) Extraction texte brut ---------------------------------------
            ext = cv_path.suffix.lower()
            if ext == ".pdf":
                plain = subprocess.check_output(
                    ["pdftotext", "-layout", str(cv_path), "-"],
                    text=True
                )
            else:
                doc   = docx.Document(str(cv_path))
                plain = "\n".join(p.text for p in doc.paragraphs)

            # 3) GPT → LaTeX --------------------------------------------------
            meta         = get_template_meta(template_id)
            template_tex = Path(meta["latex_path"]).read_text(encoding="utf-8")
            final_tex    = gpt_fill_plain(template_tex, plain, photo_name)

            # 4) Compilation --------------------------------------------------
            build = copy_template(meta)          # dossier du template
            extra = collect_assets(build)        # détecte TOUTES les images existantes

            if photo_name:                       # ajoute la photo utilisateur
                extra[photo_name] = app.config["UPLOAD_FOLDER"] / photo_name

            

            pdf_path = compile_pdf_remote(
                final_tex,
                build,
                extra_files=extra,
                compiler="xelatex"
            )

            # 5) Stocke le résultat pour /check_import_status -----------------
            async_results[session_sid] = {
                "status":       "completed",
                "photo_file":   photo_name,
                "last_tex":     final_tex,
                "pdf_filename": pdf_path
                    .relative_to(app.config["OUTPUT_FOLDER"]).as_posix()
            }

        except Exception as e:
            app.logger.error("Erreur traitement CV : %s", e)
            async_results[session_sid] = {
                "status":  "error",
                "message": str(e)
            }

        finally:
            cv_path.unlink(missing_ok=True)
            if photo_tmp:
                Path(photo_tmp).unlink(missing_ok=True)


@app.route("/import_cv", methods=["POST"])
def import_cv():
    # 0. vérifs ──────────────────────────────────────────────────────────────
    if "template_id" not in session:
        return redirect(url_for("index"))

    cv_file = request.files.get("cvfile")
    if not cv_file or not cv_file.filename:
        flash("Choisissez un fichier CV !", "warning")
        return redirect(url_for("choose_input"))

    # 1. prépare les fichiers temporaires ────────────────────────────────────
    session_sid = generate_session_sid()                # identifiant du job

    ext     = Path(cv_file.filename).suffix.lower()
    cv_tmp  = app.config["UPLOAD_FOLDER"] / f"tmp_{session_sid}{ext}"
    cv_tmp.parent.mkdir(parents=True, exist_ok=True)
    cv_file.save(cv_tmp)

    photo_tmp = ""
    photo_in  = request.files.get("photo")
    if photo_in and photo_in.filename:
        photo_tmp = (app.config["UPLOAD_FOLDER"] /
                     f"tmp_{session_sid}{Path(photo_in.filename).suffix}")
        photo_in.save(photo_tmp)

    # 2. stocke l’ID dans la session HTTP (côté client) ─────────────────────
    session["async_job_id"] = session_sid

    # 3. lance le thread : **on passe exactement les 4 args attendus** ───────
    threading.Thread(
        target=process_cv_async,
        args=(cv_tmp,            # ← 1. chemin du CV temporaire
              photo_tmp,         # ← 2. chemin éventuel de la photo
              session["template_id"],   # ← 3. id du template
              session_sid)       # ← 4. identifiant interne pour async_results
    ).start()

    # 4. retour immédiat : page spinner
    return render_template("input1_loading.html")







@app.route("/check_import_status")
def check_import_status():
    if "async_job_id" not in session:
        return jsonify({"status": "error", "message": "Session invalide"})
    
    job_id = session["async_job_id"]
    result = async_results.get(job_id)
    
    if not result:
        return jsonify({"status": "processing"})
    
    if result["status"] == "completed":
        # Stocker les résultats dans la session Flask
        session["photo_file"] = result["photo_file"]
        session["last_tex"] = result["last_tex"]
        session["pdf_filename"] = result["pdf_filename"]
        
        # Nettoyer le cache
        async_results.pop(job_id, None)
        session.pop("async_job_id", None)
        
        return jsonify({"status": "completed"})
    
    elif result["status"] == "error":
        error = result["message"]
        async_results.pop(job_id, None)
        session.pop("async_job_id", None)
        return jsonify({"status": "error", "message": error})
    
    return jsonify({"status": "processing"})# ---------------------------------------------------------------------------
#  2-B.  « FORMULAIRE »  ── Option 2 (POST rapide + AJAX)
# ---------------------------------------------------------------------------
@app.route("/minidata", methods=["GET", "POST"])
def minidata():
    """
    GET  →  affiche minidata.html
    POST →  stocke FORM + photo dans la session, redirige en <200 ms vers /loading
    """
    if "template_id" not in session:
        return redirect(url_for("index"))

    # ---------- affichage du formulaire
    if request.method == "GET":
        return render_template("minidata.html")

    # ---------- traitement ultra-rapide (on NE lance plus GPT ici)
    # 1. tout le formulaire (listes afin de ne rien perdre)
    session["raw_form"] = request.form.to_dict(flat=False)
    
    # 2. la photo
    f = request.files.get("photo")
    if f and f.filename:
        name = f"{uuid.uuid4().hex}{Path(f.filename).suffix}"
        (app.config["UPLOAD_FOLDER"] / name).parent.mkdir(parents=True, exist_ok=True)
        f.save(app.config["UPLOAD_FOLDER"] / name)
        session["photo_file"] = name
    else:
        session["photo_file"] = ""

    return redirect(url_for("loading"))           # spinner


# --------- petit spinner ----------------------------------------------------
# @app.route("/loading")
# def loading():
#     if "raw_form" not in session:
#         return redirect(url_for("minidata"))
#     return render_template("loading.html")        # voir template ci-dessous


# --------- AJAX : heavy work  (GPT + LaTeX) ---------------------------------
# @app.route("/generate", methods=["POST"])
# def generate():
#     if "raw_form" not in session:
#         return jsonify(error="no data"), 400

#     # -------- reconstituer les données du form
#     flat = {k: v[0] for k, v in session.pop("raw_form").items()}
#     flat["photo"] = session.pop("photo_file", "")

#     # tableaux dynamiques
#     flat.update(extract_arrays(flat))

#     # -------- GPT ➜ JSON complet
#     meta   = get_template_meta(session["template_id"])
#     schema = json.loads(Path(meta["latex_path"]).with_name("schema.json").read_text())
#     full   = gpt_autofill(schema, flat)

#     # -------- render & compile
#     tpl_tex = Path(meta["latex_path"]).read_text(encoding="utf-8")
#     final   = gpt_render(tpl_tex, full)
#     pdf     = compile_pdf(final, meta, full)

#     # -------- stocke pour /preview
#     session["cv_data"]      = full
#     session["last_tex"]     = final
#     session["pdf_filename"] = pdf.relative_to(app.config["OUTPUT_FOLDER"]).as_posix()

#     return jsonify(ok=True)

# @app.route("/generate", methods=["POST"])
# def generate():
#     if "template_id" not in session:
#         return jsonify(error="template"), 400

#     flat  = session.pop("raw_form")
    
#     photo = session.pop("photo_file", "")

#     placeholders = build_placeholders(flat, photo_name=photo)
#     print(placeholders)
#     meta     = get_template_meta(session["template_id"])

#     tpl_tex  = Path(meta["latex_path"]).read_text(encoding="utf-8")
#     final    = apply_placeholders(tpl_tex, placeholders)
#     print(final)
#     # ctx doit contenir 'photo' UNIQUEMENT (pas les %%…%%>)
    
#     pdf_path = compile_pdf(final, meta, {"photo": photo})
#     session["photo_file"] = photo 

#     session["last_tex"]     = final
#     session["pdf_filename"] = pdf_path.relative_to(
#                                 app.config["OUTPUT_FOLDER"]).as_posix()
#     return jsonify(ok=True)

# 



# ---------------------------------------------------------------------------
# 1️⃣  Helper : gpt_fill_latex  (remplace gpt_autofill + gpt_render)
# ---------------------------------------------------------------------------

def gpt_fill_latex(template_tex: str, data: dict) -> str:
    """Renvoie le LaTeX final en injectant *data* dans *template_tex*.

    - *data* provient directement du formulaire (y compris jobs/degrees).
    - Si le résumé professionnel est manquant ou < 20 mots, GPT rédige
      un **Professional Summary** (3 phrases max) pertinent pour le rôle
      cible (`target_role`).
    - GPT reformule les descriptions d’expériences et formations :
      • style bullet points, verbs d’action, résultats chiffrés si dispo.
    - Répond **strictement** par le code LaTeX complet, sans balises
      ``` ni commentaires.
    """

    sys = (
    # — rôle ----------------------------------------------------------
    "Tu es un expert LaTeX francophone. On te donne un template complet "
    "et les données brutes d’un formulaire de CV.\n"
   
    # — règle d’or ----------------------------------------------------
    "Règle ABSOLUE : tu n’inventes aucun contenu. Tu ne modifies ni dates, "
    "ni noms, ni nombres, ni valeurs chiffrées. Si un élément n’existe pas "
    "dans les données, tu le laisses vide ou supprimes le bloc.\n"
    # — reformulation limitée ----------------------------------------
    "Pour chaque champ « description » présent pour les experiences professionnelles et Formations, reformule uniquement la "
    "syntaxe : orthographe, tournures plus fluides avec des phrases courtes. "
    "**Ne change pas le sens ni n’ajoute de chiffres ou d’indicateurs.**\n"
    # — résumé optionnel ---------------------------------------------
    "Tu peux rédiger un résumé professionnel **seulement si le champ "
    "summary est vide** ; dans ce cas, 3–4 phrases sans chiffres ni superlatifs.\n"
    # — LaTeX ---------------------------------------------------------
    "Remplace les variables du template par les valeurs fournies, améliore "
    "la lisibilité LaTeX (\\item, sauts de ligne) et insère la photo telle quelle "
    "dans \\includegraphics.\n"
    # — sortie --------------------------------------------------------
    "Réponds STRICTEMENT avec le code LaTeX final, sans ``` ni commentaires."
)

    user = (
        "TEMPLATE:\n" + template_tex +
        "\n\nDONNÉES:\n" + json.dumps(data, ensure_ascii=False)
    )

    out = openai.ChatCompletion.create(
        model    = OPENAI_MODEL,
        messages = [
            {"role": "system", "content": sys},
            {"role": "user",   "content": user}
        ]
    ).choices[0].message.content

    # Nettoyage éventuel de ``` si le modèle en ajoute malgré l’instruction
    return re.sub(r"^```.*?\n|\n?```$", "", out, flags=re.S).strip()

# ---------------------------------------------------------------------------
# 2️⃣  Route /generate (remplace totalement l’ancienne)
# ---------------------------------------------------------------------------

# @app.route("/generate", methods=["POST"])
# def generate():
#     """Pipeline : form → GPT (LaTeX) → PDF."""
#     if "template_id" not in session or "raw_form" not in session:
#         return jsonify(error="template ou formulaire manquant"), 400

#     # 1. Récupération / préparation des données ---------------------------------
#     flat   = {k: v[0] for k, v in session.pop("raw_form").items()}
#     photo  = session.pop("photo_file", "")
#     flat["photo"] = photo

#     # Tableaux dynamiques (jobs[], degrees[], languages[], skills[], …)
#     flat.update(extract_arrays(flat))

#     # 2. Lecture du template -----------------------------------------------------
#     meta     = get_template_meta(session["template_id"])
#     template = Path(meta["latex_path"]).read_text(encoding="utf-8")

#     # 3. GPT produit le LaTeX complet -------------------------------------------
#     final_tex = gpt_fill_latex(template, flat)

#     # 4. Compilation PDF ---------------------------------------------------------
#     pdf_path = compile_pdf(final_tex, meta, {"photo": photo})

#     # 5. Stockage session --------------------------------------------------------
#     session.update({
#         "cv_data":      flat,           # éventuel usage ultérieur
#         "last_tex":     final_tex,
#         "pdf_filename": pdf_path.relative_to(app.config["OUTPUT_FOLDER"]).as_posix(),
#         "photo_file":   photo,
#     })
#     return jsonify(ok=True)


from pathlib import Path

ASSET_EXTS = {".jpg", ".jpeg", ".png", ".pdf", ".eps", ".svg"}

def collect_assets(build: Path) -> dict[str, Path]:
    """
    Parcourt récursivement *build* et renvoie
    {chemin_relatif_dans_tex : Path_absolu}.
    On inclut seulement les extensions définies dans ASSET_EXTS.
    """
    assets = {}
    for p in build.rglob("*"):
        if p.is_file() and p.suffix.lower() in ASSET_EXTS:
            rel = p.relative_to(build).as_posix()   # ex. "img/bg.png"
            assets[rel] = p
    return assets



# import requests                                   # ajouté dans requirements.txt

# @app.route("/generate", methods=["POST"])
# def generate():
#     if "template_id" not in session or "raw_form" not in session:
#         return jsonify(error="template ou formulaire manquant"), 400

#     # ---------- 1. données formulaire
#     flat   = {k: v[0] for k, v in session.pop("raw_form").items()}
#     photo  = session.pop("photo_file", "")
#     flat["photo"] = photo
#     flat.update(extract_arrays(flat))

#     # ---------- 2. template LaTeX + GPT
#     meta     = get_template_meta(session["template_id"])
#     template = Path(meta["latex_path"]).read_text(encoding="utf-8")
#     final_tex = gpt_fill_latex(template, flat)

#     build = copy_template(meta)           # dossier de travail (copie du template)

#     extra = collect_assets(build)         # ← ① assets trouvés dans le template

#     if photo:                                           # ajoute la photo si fournie
#         extra[photo] = app.config["UPLOAD_FOLDER"] / photo

#     pdf_path = compile_pdf_remote(
#         final_tex,
#         build,
#         extra_files=extra,
#         compiler="xelatex"      # ou "lualatex" si tu préfères
#     )

#     # ---------- 4. session
#     session.update({
#         "cv_data":      flat,
#         "last_tex":     final_tex,
#         "pdf_filename": pdf_path.relative_to(app.config["OUTPUT_FOLDER"]).as_posix(),
#         "photo_file":   photo,
#     })
#     return jsonify(ok=True)


#######################################################################
# Ajouter en haut de app.py
import threading
from collections import defaultdict

# Stocker les résultats des tâches
async_results_generate = defaultdict(dict)


@app.route("/generate", methods=["POST"])
def generate():
    if "template_id" not in session or "raw_form" not in session:
        return jsonify(error="template ou formulaire manquant"), 400
    
    # Créer un ID de session unique
    session_sid = generate_session_sid()
    session["async_job_id"] = session_sid

    # Démarrer le traitement dans un thread séparé
    threading.Thread(
        target=process_generation_async,
        args=(session_sid, session.copy())  # Copie des données de session
    ).start()

    return jsonify(status="processing", job_id=session_sid)


################################################################################


def process_generation_async(session_sid, session_data):
    with app.app_context():
        try:
            # Récupérer les données de la session
            flat = {k: v[0] for k, v in session_data["raw_form"].items()}
            photo = session_data.get("photo_file", "")
            flat["photo"] = photo
            flat.update(extract_arrays(flat))

            # Traitement (identique à votre ancienne fonction generate)
            meta = get_template_meta(session_data["template_id"])
            template = Path(meta["latex_path"]).read_text(encoding="utf-8")
            final_tex = gpt_fill_latex(template, flat)
            
            build = copy_template(meta)
            extra = collect_assets(build)
            if photo:
                extra[photo] = app.config["UPLOAD_FOLDER"] / photo

            pdf_path = compile_pdf_remote(
                final_tex,
                build,
                extra_files=extra,
                compiler="xelatex"
            )

            # Stocker le résultat
            async_results[session_sid] = {
                "status": "completed",
                "pdf_filename": pdf_path.relative_to(app.config["OUTPUT_FOLDER"]).as_posix(),
                "last_tex": final_tex,
                "photo_file": photo
            }
            

        except Exception as e:
            async_results[session_sid] = {
                "status": "error",
                "message": str(e)
            }

#####################################################################################

# Remplacer la route existante par :
@app.route("/check_status")
def check_status():
    job_id = session.get("async_job_id")
    if not job_id:
        return jsonify(status="error", message="Job ID manquant"), 400
    
    result = async_results.get(job_id, {})
    
    if not result:
        return jsonify(status="processing")
    
    if result["status"] == "completed":
        # Mettre à jour la session
        session["pdf_filename"] = result["pdf_filename"]
        session["last_tex"] = result["last_tex"]
        session["photo_file"] = result["photo_file"]
        
        # Nettoyer
        async_results.pop(job_id, None)
        session.pop("async_job_id", None)
        return jsonify(status="completed")
    
    return jsonify(status=result["status"], message=result.get("message", ""))

#####################################################################################


@app.route("/loading")
def loading():
    job_id = request.args.get("job_id", session.get("async_job_id", ""))
    return render_template("loading.html", job_id=job_id)

#####################################################################################


# ---------------------------------------------------------------------------
# 3️⃣  Nettoyage à prévoir
# ---------------------------------------------------------------------------
# • Supprimez ou commentez toutes les anciennes fonctions liées aux placeholders
#   (build_placeholders, apply_placeholders, PLACEHOLDER_PAT…).
# • `gpt_autofill` n’est plus nécessaire.
# • Le reste de l’app (routes /loading, /preview, /edit…) reste inchangé.



# ---------------------------------------------------------------------------
#  Aperçu  (dans l’option 2, on arrive ici via JS après succès de /generate)
# ---------------------------------------------------------------------------



@app.route("/form")
def legacy_form():
    # redirige l’ancien endpoint vers le nouveau
    return redirect(url_for("minidata"))


# @app.route("/preview")
# def preview():
#     if "pdf_filename" not in session:             # rien de prêt → retour
#         return redirect(url_for("minidata"))
#     return render_template("preview.html",
#                            pdf_filename=session["pdf_filename"])


# @app.route("/preview")
# def preview():
#     if "template_id" not in session:
#         return redirect(url_for("index"))

#     meta = get_template_meta(session["template_id"])

#     # 1️⃣  on regarde d’abord s’il y a une version éditée
#     if "last_tex" in session:
#         final_tex = session["last_tex"]
#     else:
#         template_tex = Path(meta["latex_path"]).read_text(encoding="utf-8")
#         final_tex    = gpt_render(template_tex, session["cv_data"])
#         session["last_tex"] = final_tex          # on la garde pour plus tard

#     # 2️⃣  compile uniquement si le PDF n’a pas déjà été produit
#     if "pdf_filename" not in session:
#         print("1")
#         pdf_path = compile_pdf(final_tex, meta, session["cv_data"])
#         session["pdf_filename"] = pdf_path.relative_to(
#             app.config["OUTPUT_FOLDER"]
#         ).as_posix()
#     print("0")
#     return render_template("preview.html",
#                            pdf_filename=session["pdf_filename"])

# @app.route("/preview")
# def preview():
#     # sécurité minimale
#     if "template_id" not in session or "cv_data" not in session:
#         return redirect(url_for("index"))

#     meta = get_template_meta(session["template_id"])

#     # 1. quel LaTeX utiliser ?
#     latex_src = session.get("last_tex")
#     if latex_src is None:                          # première fois
#         template_tex = Path(meta["latex_path"]).read_text(encoding="utf-8")
#         latex_src    = gpt_render(template_tex, session["cv_data"])

#     # 2. compile À CHAQUE APPEL
#     pdf_path = compile_pdf(latex_src, meta, session["cv_data"])

#     # 3. met à jour la session (toujours)
#     session["last_tex"]     = latex_src
#     session["pdf_filename"] = pdf_path.relative_to(
#         app.config["OUTPUT_FOLDER"]).as_posix()

#     # 4. time-stamp pour l’anti-cache
#     return render_template("preview.html",
#                            pdf_filename=session["pdf_filename"],
#                            ts=int(uuid.uuid4().int % 1e6))     # petit nombre aléatoire

# @app.route("/preview")
# def preview():
#     if "last_tex" not in session:
#         return redirect(url_for("index"))

#     meta  = get_template_meta(session["template_id"])
#     photo = session.get("photo_file", "")

#     # ➜ on repasse le nom du fichier photo stocké juste après /minidata
#     pdf   = compile_pdf_remote(session["last_tex"],
#                         meta,
#                         {"photo": photo})

#     session["pdf_filename"] = pdf.relative_to(app.config["OUTPUT_FOLDER"]).as_posix()
#     return render_template("preview.html",
#                            pdf_filename=session["pdf_filename"],
#                            ts=uuid.uuid4().hex)        # anti-cache

# utils_assets.py  (ou en haut de app.py)



@app.route("/preview")
def preview():
    if "last_tex" not in session:
        return redirect(url_for("index"))

    meta   = get_template_meta(session["template_id"])
    photo  = session.get("photo_file", "")

    build = copy_template(meta)           # dossier de travail (copie du template)

    extra = collect_assets(build)         # ← ① assets trouvés dans le template

    final_tex=session["last_tex"]

    # ② on ajoute la photo envoyée par l’utilisateur, le cas échéant
    if photo:
        extra[photo] = app.config["UPLOAD_FOLDER"] / photo

    pdf = compile_pdf_remote(
        final_tex,
        build,
        extra_files=extra,                # ← la liste complète
        compiler="xelatex"
    )

    session["pdf_filename"] = pdf.relative_to(app.config["OUTPUT_FOLDER"]).as_posix()
    return render_template("preview.html",
                           pdf_filename=session["pdf_filename"],
                           ts=uuid.uuid4().hex)


# ---------------------------------------------------------------------------
#  Fichier PDF / Téléchargement – inchangés
# ---------------------------------------------------------------------------
# @app.route("/file/<path:filename>")
# def file(filename):
#     return send_file(app.config["OUTPUT_FOLDER"] / filename)

@app.route("/file/<path:filename>")
def file(filename):
    """Servez le PDF + entêtes anti-cache."""
    resp = send_file(app.config["OUTPUT_FOLDER"] / filename, conditional=False)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp





# 
@app.route("/download")
def download():
    if "pdf_filename" not in session:
        return redirect(url_for("preview"))
    return send_file(app.config["OUTPUT_FOLDER"]
                     / session["pdf_filename"], as_attachment=True)


# ---------------------------------------------------------------------------
#  Édition libre (inchangé)
# ---------------------------------------------------------------------------
# ...  (gardez votre fonction gpt_edit et la route /edit telles quelles)


def gpt_edit(tex: str, instr: str) -> str:
    """Applique *instr* au code LaTeX *tex* sans rien inventer.

    * L’instruction est libre (supprimer, modifier, ajouter une ligne, etc.).
    * Aucune autre partie du document ne doit être altérée.
    * La sortie doit être le **LaTeX complet**, sans balises ``` ni commentaires.
    """

    sys = (
        "Tu es un expert en LaTeX. Tu reçois le code LaTeX complet d’un CV et une instruction unique. "
        "Applique strictement cette instruction, sans rien inventer d’autre. "
        "Ne modifie pas les parties non concernées et conserve tous les encodages/accents. "
        "Réponds UNIQUEMENT avec le LaTeX final, sans balises ``` ni commentaires supplémentaires."
    )

    user = f"INSTRUCTION : {instr}\n\nCODE LATEX :\n{tex}"

    out = openai.ChatCompletion.create(
        model       = OPENAI_MODEL,
        messages    = [
            {"role": "system", "content": sys},
            {"role": "user",   "content": user}
        ]
        
    ).choices[0].message.content

    # sécurité : retire tout bloc ``` éventuel
    return re.sub(r"^```.*?\n|\n?```$", "", out, flags=re.S).strip()

# ---------------------------------------------------------------------------
# 1️⃣  Phase rapide : on stocke l’instruction et on affiche le spinner
# ---------------------------------------------------------------------------
@app.route("/edit_prepare", methods=["POST"])
def edit_prepare():
    if "template_id" not in session or "last_tex" not in session:
        return redirect(url_for("preview"))

    instr = request.form.get("instruction", "").strip()
    if not instr:
        flash("Veuillez saisir une instruction !", "warning")
        return redirect(url_for("preview"))

    session["pending_edit"] = instr  # sera consommé par /edit_apply
    return render_template("edit_loading.html")  # spinner immédiat

# ---------------------------------------------------------------------------
# 2️⃣  Phase lourde : appelée en AJAX depuis edit_loading.html
# ---------------------------------------------------------------------------
@app.route("/edit_apply", methods=["POST"])
def edit_apply():
    if "pending_edit" not in session or "last_tex" not in session:
        return jsonify(error="no edit"), 400

    instr = session.pop("pending_edit")
    old_tex = session["last_tex"]
    new_tex = gpt_edit(old_tex, instr)

    meta   = get_template_meta(session["template_id"])
    build  = copy_template(meta)
    extra  = collect_assets(build)

    photo = session.get("photo_file", "")
    if photo:
        extra[photo] = app.config["UPLOAD_FOLDER"] / photo

    # 🛠️ Correction ici : on passe `build` en premier
    pdf = compile_pdf_remote(new_tex, build, extra_files=extra)

    session["last_tex"] = new_tex
    session["pdf_filename"] = pdf.relative_to(app.config["OUTPUT_FOLDER"]).as_posix()

    return jsonify(ok=True)


@app.route("/import_prepare", methods=["POST"])
def import_prepare():
    if "template_id" not in session:
        return redirect(url_for("index"))

    cv_file   = request.files.get("cvfile")
    photo_file = request.files.get("photo")
    if not cv_file or not cv_file.filename:
        flash("Choisissez un fichier CV !", "warning")
        return redirect(url_for("choose_input"))

    # Sauvegarde temporaire ---------------------------------------------------
    ext = Path(cv_file.filename).suffix.lower()
    cv_tmp = app.config["UPLOAD_FOLDER"] / f"tmp_{uuid.uuid4().hex}{ext}"
    cv_tmp.parent.mkdir(parents=True, exist_ok=True)
    cv_file.save(cv_tmp)

    photo_tmp = ""
    if photo_file and photo_file.filename:
        photo_tmp = app.config["UPLOAD_FOLDER"] / f"tmp_{uuid.uuid4().hex}{Path(photo_file.filename).suffix}"
        photo_file.save(photo_tmp)

    # Enregistre dans la session ---------------------------------------------
    session["pending_cv"]    = cv_tmp.as_posix()
    session["pending_photo"] = photo_tmp.as_posix() if photo_tmp else ""

    return render_template("loading.html")  # spinner immédiat



# ---------------------------------------------------------------------------
# Étape 2 : travail lourd en AJAX -------------------------------------------
# ---------------------------------------------------------------------------
@app.route("/import_apply", methods=["POST"])
def import_apply():
    if "pending_cv" not in session:
        return jsonify(error="no pending"), 400

    cv_path   = Path(session.pop("pending_cv"))
    photo_tmp = session.pop("pending_photo", "")
    photo_name = ""

    # Copie la photo définitive ----------------------------------------------
    if photo_tmp:
        photo_name = f"{uuid.uuid4().hex}{Path(photo_tmp).suffix}"
        dest = app.config["UPLOAD_FOLDER"] / photo_name
        shutil.move(photo_tmp, dest)

    # Extraction texte brut ---------------------------------------------------
    ext = cv_path.suffix.lower()
    if ext == ".pdf":
        plain = subprocess.check_output(["pdftotext", "-layout", str(cv_path), "-"]).decode("utf-8", errors="ignore")
    else:
        doc = docx.Document(str(cv_path))
        plain = "".join(p.text for p in doc.paragraphs)

    # GPT → LaTeX -------------------------------------------------------------
    meta         = get_template_meta(session["template_id"])
    template_tex = Path(meta["latex_path"]).read_text(encoding="utf-8")
    final_tex    = gpt_fill_plain(template_tex, plain, photo_name)
    print(final_tex)
    # Compilation -------------------------------------------------------------
    pdf_path = compile_pdf_remote(final_tex, meta, {"photo": photo_name})

    # Session update ----------------------------------------------------------
    session.update({
        "photo_file":  photo_name,
        "last_tex":     final_tex,
        "pdf_filename": pdf_path.relative_to(app.config["OUTPUT_FOLDER"]).as_posix(),
    })

    # Nettoyage du fichier temporaire
    cv_path.unlink(missing_ok=True)

    return jsonify(ok=True)




# @app.route("/edit", methods=["POST"])
# def edit():
#     # # aucun LaTeX en session ⇒ retour à l’aperçu
#     if "last_tex" not in session:
#         return redirect(url_for("preview"))

#     instruction = request.form.get("instruction", "").strip()
#     print(instruction)
#     if not instruction:
#         flash("Veuillez saisir une instruction !", "warning")
#         return redirect(url_for("preview"))

#     try:
#         # 1. GPT applique la modification
#         new_tex = gpt_edit(session["last_tex"], instruction)
        
#         # 2. On recompile
#         meta = get_template_meta(session["template_id"])
#         pdf_path = compile_pdf(new_tex, meta, session["cv_data"])

#         # 3. On met à jour la session
#         session["last_tex"] = new_tex
#         session["pdf_filename"] = pdf_path.relative_to(
#             app.config["OUTPUT_FOLDER"]
#         ).as_posix()

#         flash("Modification appliquée !", "success")

#     except Exception as exc:
#         flash(f"Erreur GPT/LaTeX : {exc}", "danger")

#     return redirect(url_for("preview"))


@app.route("/edit", methods=["POST"])
def edit():
    if "template_id" not in session or "last_tex" not in session:
        return redirect(url_for("preview"))

    instr = request.form.get("instruction", "").strip()
    if not instr:
        flash("Veuillez saisir une instruction !", "warning")
        return redirect(url_for("preview"))

    # 1 : GPT applique l’instruction
    session["last_tex"] = gpt_edit(session["last_tex"], instr)

    flash("Modification appliquée !", "success")
    return redirect(url_for("preview"))     # 👉 preview() re-compi­lera



# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)
