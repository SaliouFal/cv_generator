

######################################################################


# ─────────────────────────────────────────────────────────────────────────────
# app.py  –  version « split 30 s » (Heroku friendly)
# ─────────────────────────────────────────────────────────────────────────────
import os, json, uuid, shutil, subprocess, re, textwrap
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, send_file, flash, jsonify
)
import openai

# ───────────────────── Configuration ──────────────────────
openai.api_key = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "o3")

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


def compile_pdf(tex: str, meta: dict, ctx: dict) -> Path:
    build = copy_template(meta)
    tex_path = build / "main.tex"
    tex_path.write_text(tex, encoding="utf-8")

    # éventuelle copie de la photo …
    if (photo := ctx.get("photo")):
        up = app.config["UPLOAD_FOLDER"] / photo
        if up.is_file():
            shutil.copy(up, build / photo)

    #engine = "pdflatex"
    engine = "xelatex"
    # if re.search(r"\\usepackage\{fontspec\}", tex) \
    #    or re.search(r"!TEX program *= *xelatex", tex, flags=re.I):
    #     engine = "xelatex"

    # ── on ne demande PAS à Python de décoder (pas de text=True) ──────────
    proc = subprocess.run(
        [engine, "-interaction=nonstopmode", tex_path.name],
        cwd=build,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )

    # on décode nous-mêmes, sans lever d’exception UnicodeDecodeError
    log = proc.stdout.decode("utf-8", errors="ignore")

    pdf = build / "main.pdf"
    if proc.returncode != 0 or not pdf.exists():
        raise RuntimeError(f"La compilation {engine} a échoué :\n" + log[-1500:])

    return pdf


def gpt_render(template_tex:str, data:dict)->str:
    """Injecte les données + améliore la mise en page, renvoie le LaTeX final."""
    sys = ("Tu es un expert LaTeX en Français donc les cv doivent etre en français c'est important. On te donne un template et un objet JSON. "
      "Remplace toutes les variables/structures par les valeurs JSON. "
      "N'invente pas les champs déjà fournis (nom, email, téléphone, jobs.dates...). "
      "Ajoute descriptions, compétences, profil, skills, hobbies, Education si manquants, "
      "et améliore la lisibilité (sauts de ligne, \\item, etc.). "
      "Le champ 'photo' doit être inséré tel quel dans \\includegraphics. "
      "Renvoie UNIQUEMENT le code LaTeX final, sans ```.")
    

    user = ("TEMPLATE:\n" + template_tex +
            "\n\nDONNÉES:\n" + json.dumps(data, ensure_ascii=False, indent=2))
    out  = openai.ChatCompletion.create(model=OPENAI_MODEL,
             messages=[{"role":"system","content":sys},{"role":"user","content":user}]
           ).choices[0].message.content
    
    return re.sub(r"^```.*?\\n|\\n?```$", "", out, flags=re.S).strip()





def extract_arrays(form) -> dict:
    """jobs[0][title] → {'jobs':[...], 'degrees':[...]}  (ordre conservé)."""
    buf = {}
    for k, v in form.items():
        m = array_re.fullmatch(k)
        if m:
            fld, idx, key = m.group("fld"), int(m.group("idx")), m.group("key")
            buf.setdefault((fld, idx), {})[key] = v.strip()

    out = {"jobs": [], "degrees": []}
    for (fld, idx), row in sorted(buf.items(), key=lambda p: p[0][1]):
        out[fld].append(row)
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


# ---------------------------------------------------------------------------
#  2-A.  « IMPORTER » un CV PDF / DOCX  →  inchangé (travail rapide)
# ---------------------------------------------------------------------------
@app.route("/import_cv", methods=["POST"])
def import_cv():
    # (code identique à votre version – pas modifié)
    # …
    return redirect(url_for("preview"))


# ---------------------------------------------------------------------------
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
@app.route("/loading")
def loading():
    if "raw_form" not in session:
        return redirect(url_for("minidata"))
    return render_template("loading.html")        # voir template ci-dessous


# --------- AJAX : heavy work  (GPT + LaTeX) ---------------------------------
@app.route("/generate", methods=["POST"])
def generate():
    if "raw_form" not in session:
        return jsonify(error="no data"), 400

    # -------- reconstituer les données du form
    flat = {k: v[0] for k, v in session.pop("raw_form").items()}
    flat["photo"] = session.pop("photo_file", "")

    # tableaux dynamiques
    flat.update(extract_arrays(flat))

    # -------- GPT ➜ JSON complet
    meta   = get_template_meta(session["template_id"])
    schema = json.loads(Path(meta["latex_path"]).with_name("schema.json").read_text())
    full   = gpt_autofill(schema, flat)

    # -------- render & compile
    tpl_tex = Path(meta["latex_path"]).read_text(encoding="utf-8")
    final   = gpt_render(tpl_tex, full)
    pdf     = compile_pdf(final, meta, full)

    # -------- stocke pour /preview
    session["cv_data"]      = full
    session["last_tex"]     = final
    session["pdf_filename"] = pdf.relative_to(app.config["OUTPUT_FOLDER"]).as_posix()

    return jsonify(ok=True)


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

@app.route("/preview")
def preview():
    # sécurité minimale
    if "template_id" not in session or "cv_data" not in session:
        return redirect(url_for("index"))

    meta = get_template_meta(session["template_id"])

    # 1. quel LaTeX utiliser ?
    latex_src = session.get("last_tex")
    if latex_src is None:                          # première fois
        template_tex = Path(meta["latex_path"]).read_text(encoding="utf-8")
        latex_src    = gpt_render(template_tex, session["cv_data"])

    # 2. compile À CHAQUE APPEL
    pdf_path = compile_pdf(latex_src, meta, session["cv_data"])

    # 3. met à jour la session (toujours)
    session["last_tex"]     = latex_src
    session["pdf_filename"] = pdf_path.relative_to(
        app.config["OUTPUT_FOLDER"]).as_posix()

    # 4. time-stamp pour l’anti-cache
    return render_template("preview.html",
                           pdf_filename=session["pdf_filename"],
                           ts=int(uuid.uuid4().int % 1e6))     # petit nombre aléatoire











# ---------------------------------------------------------------------------
#  Fichier PDF / Téléchargement – inchangés
# ---------------------------------------------------------------------------
@app.route("/file/<path:filename>")
def file(filename):
    return send_file(app.config["OUTPUT_FOLDER"] / filename)

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


# ───────────────────── Édition libre ----------------------------------------
def gpt_edit(tex,instr):
    sys="Tu es un expert LaTeX. Applique l'instruction et renvoie le résultat."
    out=openai.ChatCompletion.create(model=OPENAI_MODEL,
        messages=[{"role":"system","content":sys},
                  {"role":"user","content":f"Instruction : {instr} \n---\n{tex}"}]
        ).choices[0].message.content
    return re.sub(r"^```.*?\n|\n?```$", "", out, flags=re.S)


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

    try:
        # 1️⃣ GPT applique l’instruction
        new_tex = gpt_edit(session["last_tex"], instr)
        print(new_tex)
        # 2️⃣ Recompile systématiquement
        meta     = get_template_meta(session["template_id"])
        pdf_path = compile_pdf(new_tex, meta, session["cv_data"])

        # 3️⃣ Met à jour la session
        session["last_tex"]     = new_tex
        session["pdf_filename"] = pdf_path.relative_to(
            app.config["OUTPUT_FOLDER"]).as_posix()

        flash("Modification appliquée !", "success")
    except Exception as e:
        flash(f"Erreur GPT/LaTeX : {e}", "danger")

    return redirect(url_for("preview"))






# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)

