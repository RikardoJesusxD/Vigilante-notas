"""
Vigilante de notas - SIGA Web Alumno (Universidad Nacional del Santa).

Entra con tu cuenta, lee "Registro de Calificaciones", compara con la
revision anterior y, si hay una nota nueva, te manda un aviso al celular
con ntfy.

IMPORTANTE: este repositorio es publico, asi que este script NO imprime
notas ni datos personales en los logs. Todo lo sensible viaja solo a ntfy.
"""
import json
import os
import re
import urllib.request
from pathlib import Path

ESTADO = Path("estado.json")
URLS_LOGIN = [
    "http://registro.uns.edu.pe:80/WebAlumno/faces/login.xhtml",
    "https://registro.uns.edu.pe/WebAlumno/faces/login.xhtml",
]
ALERTA_TRAS_ERRORES = 4  # avisa si falla 4 veces seguidas (~1 hora)

JS_EXTRAER = r"""
() => {
  const codigo = /^\d+-\d+$/;
  const filas = [...document.querySelectorAll('tr')].filter(tr =>
    [...tr.children].some(td => codigo.test(td.innerText.trim())));
  if (!filas.length) return {cabeceras: [], filas: []};
  const cont = filas[0].closest('.ui-datatable') || filas[0].closest('table');
  const grid = [];
  [...cont.querySelectorAll('thead tr')].forEach((tr, r) => {
    grid[r] = grid[r] || [];
    let c = 0;
    [...tr.children].forEach(th => {
      while (grid[r][c] !== undefined) c++;
      const cs = th.colSpan || 1, rs = th.rowSpan || 1;
      const txt = th.innerText.trim().replace(/\s+/g, ' ');
      for (let i = 0; i < rs; i++) {
        grid[r + i] = grid[r + i] || [];
        for (let j = 0; j < cs; j++) grid[r + i][c + j] = txt;
      }
      c += cs;
    });
  });
  const n = filas[0].children.length;
  const cabeceras = [];
  for (let c = 0; c < n; c++) {
    const partes = [];
    grid.forEach(row => {
      const t = row[c];
      if (t && !partes.includes(t)) partes.push(t);
    });
    cabeceras.push(partes.join(' '));
  }
  return {
    cabeceras,
    filas: filas.map(tr => [...tr.children].map(td => td.innerText.trim())),
  };
}
"""


def secreto(nombre):
    valor = os.environ.get(nombre, "").strip()
    if not valor:
        raise SystemExit(f"Falta el secreto {nombre} en GitHub (Settings > Secrets).")
    return valor


def avisar(titulo, mensaje, prioridad="default"):
    """Manda una notificacion a tu celular mediante ntfy.sh."""
    tema = secreto("NTFY_TOPIC")
    req = urllib.request.Request(
        f"https://ntfy.sh/{tema}",
        data=mensaje.encode("utf-8"),
        headers={"Title": titulo, "Priority": prioridad, "Tags": "mortar_board"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30).read()


def leer_notas():
    """Inicia sesion en el SIGA y devuelve {'cabeceras': [...], 'filas': [[...]]}."""
    from playwright.sync_api import sync_playwright

    codigo, dni, clave = secreto("UNS_CODIGO"), secreto("UNS_DNI"), secreto("UNS_PASS")

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        ctx = navegador.new_context(locale="es-PE", viewport={"width": 1400, "height": 900})
        ctx.set_default_timeout(45000)
        page = ctx.new_page()

        abierto = False
        for url in URLS_LOGIN:
            try:
                page.goto(url, wait_until="domcontentloaded")
                abierto = True
                break
            except Exception:
                continue
        if not abierto:
            raise RuntimeError("No se pudo abrir la pagina de login (sitio caido?)")

        page.get_by_placeholder(re.compile("CODIGO", re.I)).fill(codigo)
        page.get_by_placeholder(re.compile("DNI", re.I)).fill(dni)
        page.get_by_placeholder(re.compile("CONTRASE", re.I)).fill(clave)
        page.get_by_text(re.compile("VALIDAR ACCESO", re.I)).first.click()
        try:
            page.wait_for_url(re.compile("bienvenido"), timeout=45000)
        except Exception:
            raise RuntimeError("El login no avanzo (datos incorrectos o cambio la pagina)")

        base = page.url.split("/WebAlumno/")[0]
        try:
            page.get_by_text("NOTAS", exact=True).first.click()
            page.get_by_text(re.compile("Registro de Calificaci")).first.click()
            page.wait_for_url(re.compile("notasRegistro"), timeout=20000)
        except Exception:
            page.goto(base + "/WebAlumno/faces/pages/notasRegistro.xhtml",
                      wait_until="domcontentloaded")

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_selector("tbody tr td")

        datos = page.evaluate(JS_EXTRAER)
        navegador.close()

    if not datos["filas"]:
        raise RuntimeError("No encontre la tabla de notas en la pagina")
    return datos


def a_estado(datos):
    """Convierte la tabla en {'curso': {'columna': 'valor'}} solo con celdas con valor."""
    cab = datos["cabeceras"]
    estado = {}
    for fila in datos["filas"]:
        i = next((k for k, v in enumerate(fila) if re.fullmatch(r"\d+-\d+", v)), 0)
        nombre = fila[i + 1] if i + 1 < len(fila) else ""
        curso = f"{fila[i]} {nombre}".strip()
        valores = {}
        for k, v in enumerate(fila):
            if k in (i, i + 1) or not v:
                continue
            etiqueta = cab[k] if k < len(cab) and cab[k] else f"columna {k + 1}"
            if etiqueta in valores:
                etiqueta = f"{etiqueta} ({k + 1})"
            valores[etiqueta] = v
        estado[curso] = valores
    return estado


def comparar(antes, ahora):
    """Devuelve una lista de frases con lo que cambio o aparecio."""
    cambios = []
    for curso, vals in ahora.items():
        nombre = curso.split(" ", 1)[-1]
        previo = antes.get(curso)
        if previo is None:
            cambios.append(f"{nombre}: curso nuevo en la lista")
            continue
        for etiqueta, valor in vals.items():
            viejo = previo.get(etiqueta)
            if viejo != valor:
                extra = f" (antes {viejo})" if viejo else ""
                cambios.append(f"{nombre} - {etiqueta}: {valor}{extra}")
    return cambios


def guardar(estado):
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")


def main():
    guardado = json.loads(ESTADO.read_text(encoding="utf-8")) if ESTADO.exists() else {}

    try:
        notas = a_estado(leer_notas())
    except Exception as e:
        errores = guardado.get("errores", 0) + 1
        guardado["errores"] = errores
        guardar(guardado)
        detalle = (str(e).splitlines() or [""])[0][:200]
        print(f"Error {errores} seguido(s): {type(e).__name__}: {detalle}")
        if errores == ALERTA_TRAS_ERRORES:
            avisar(
                "El vigilante de notas tiene problemas",
                f"Fallo {errores} veces seguidas. Ultimo error: {detalle}",
            )
        return

    anterior = guardado.get("notas")
    if anterior is None:
        avisar(
            "Vigilante de notas activado",
            f"Lei {len(notas)} cursos. Te avisare cuando aparezca una nota nueva.",
        )
        print("Primera lectura guardada.")
    else:
        cambios = comparar(anterior, notas)
        print(f"Revision OK. Cambios detectados: {len(cambios)}")
        if cambios:
            avisar("Nueva nota en el SIGA", "\n".join(cambios[:15]), "high")

    # Se guarda al final: si el aviso falla, se reintenta en la siguiente vuelta.
    guardar({"notas": notas, "errores": 0})


if __name__ == "__main__":
    main()
