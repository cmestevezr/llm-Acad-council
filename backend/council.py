"""Orquestación del Consejo Académico LLM en 3 etapas.

Reemplaza el council.py original de karpathy/llm-council.
Mantiene la misma interfaz pública (run_full_council, generate_conversation_title,
parse_ranking_from_text, calculate_aggregate_rankings) para ser un reemplazo
directo sin tocar main.py ni el frontend.

Etapa 1: cada modelo asume un ROL de revisor académico y evalúa el insumo
         según la rúbrica del MODO seleccionado (proyecto, manuscrito,
         doctorado, acreditacion).
Etapa 2: revisión por pares anonimizada de los informes (meta-revisión).
Etapa 3: el Presidente del Comité sintetiza un dictamen estructurado.
"""

import asyncio
import re
from typing import List, Dict, Any, Tuple

from .openrouter import query_model
from .config import (
    COUNCIL_MODELS,
    CHAIRMAN_MODEL,
    FAST_MODEL,
    REVIEWER_ROLES,
    ACADEMIC_MODES,
    DEFAULT_MODE,
    COMMON_GUARDRAILS,
)

# ---------------------------------------------------------------------------
# Detección de modo
# ---------------------------------------------------------------------------

MODE_TAG_RE = re.compile(r"\[\s*modo\s*:\s*([a-záéíóúñ_]+)\s*\]", re.IGNORECASE)

MODE_ALIASES = {
    "proyecto": "proyecto", "project": "proyecto", "grant": "proyecto",
    "manuscrito": "manuscrito", "paper": "manuscrito", "articulo": "manuscrito",
    "artículo": "manuscrito", "manuscript": "manuscrito",
    "doctorado": "doctorado", "tesis": "doctorado", "phd": "doctorado",
    "doctoral": "doctorado", "tutoria": "doctorado", "tutoría": "doctorado",
    "acreditacion": "acreditacion", "acreditación": "acreditacion",
    "accreditation": "acreditacion", "programa": "acreditacion",
}


def extract_explicit_mode(user_query: str) -> Tuple[str, str]:
    """Busca una etiqueta [modo: x] al inicio del mensaje.

    Returns:
        (mode or "", query sin la etiqueta)
    """
    match = MODE_TAG_RE.search(user_query)
    if match:
        raw = match.group(1).lower()
        mode = MODE_ALIASES.get(raw, "")
        cleaned = MODE_TAG_RE.sub("", user_query, count=1).strip()
        if mode in ACADEMIC_MODES:
            return mode, cleaned
        return "", cleaned
    return "", user_query


def detect_mode_by_keywords(user_query: str) -> str:
    """Heurística simple por palabras clave. Devuelve "" si es ambiguo."""
    text = user_query.lower()
    scores = {}
    for mode, spec in ACADEMIC_MODES.items():
        scores[mode] = sum(1 for kw in spec["keywords"] if kw in text)
    best = max(scores, key=scores.get)
    # exigir señal clara y sin empate
    top = scores[best]
    if top == 0:
        return ""
    if sorted(scores.values(), reverse=True)[1:2] == [top]:
        return ""  # empate
    return best


async def detect_mode_with_llm(user_query: str) -> str:
    """Clasificación con un modelo rápido cuando las heurísticas no bastan."""
    options = ", ".join(ACADEMIC_MODES.keys())
    prompt = (
        "Clasifica la siguiente solicitud académica en exactamente UNA de "
        f"estas categorías: {options}.\n"
        "- proyecto: evaluación de propuestas/proyectos de investigación para financiamiento.\n"
        "- manuscrito: revisión por pares de papers, artículos o abstracts.\n"
        "- doctorado: tutoría/retroalimentación de tesis o avances doctorales.\n"
        "- acreditacion: evaluación de programas académicos contra estándares de calidad.\n"
        "Responde SOLO con la palabra de la categoría, sin nada más.\n\n"
        f"Solicitud (puede estar truncada):\n{user_query[:3000]}"
    )
    response = await query_model(FAST_MODEL, [{"role": "user", "content": prompt}], timeout=30.0)
    if response is None:
        return DEFAULT_MODE
    answer = response.get("content", "").strip().lower()
    for token in ACADEMIC_MODES:
        if token in answer:
            return token
    return DEFAULT_MODE


async def resolve_mode(user_query: str) -> Tuple[str, str]:
    """Resuelve el modo de evaluación y devuelve (mode, query limpia)."""
    mode, cleaned = extract_explicit_mode(user_query)
    if mode:
        return mode, cleaned
    mode = detect_mode_by_keywords(cleaned)
    if mode:
        return mode, cleaned
    mode = await detect_mode_with_llm(cleaned)
    return mode, cleaned


# ---------------------------------------------------------------------------
# Etapa 1: informes individuales por rol
# ---------------------------------------------------------------------------

def build_reviewer_prompt(role: Dict[str, str], mode: str, user_query: str) -> str:
    spec = ACADEMIC_MODES[mode]
    return f"""{role['persona']}

Formas parte de un comité académico multidisciplinario en modalidad:
**{spec['nombre']}**.

{COMMON_GUARDRAILS}

Rúbrica de evaluación para esta modalidad:
{spec['rubrica']}

Evalúa el siguiente insumo desde tu rol de {role['nombre']}. Profundiza en
los aspectos de tu especialidad, pero puedes señalar brevemente problemas
graves fuera de ella. Sé específico: cita secciones, frases o datos del
insumo al fundamentar cada observación.

=== INSUMO A EVALUAR ===
{user_query}
=== FIN DEL INSUMO ===

Emite tu informe de revisión:"""


async def stage1_collect_responses(user_query: str, mode: str = None) -> List[Dict[str, Any]]:
    """Etapa 1: informes individuales de cada revisor (modelo + rol)."""
    if mode is None:
        mode, user_query = await resolve_mode(user_query)

    # Asignación cíclica de roles a modelos
    assignments = [
        (model, REVIEWER_ROLES[i % len(REVIEWER_ROLES)])
        for i, model in enumerate(COUNCIL_MODELS)
    ]

    async def ask(model: str, role: Dict[str, str]):
        prompt = build_reviewer_prompt(role, mode, user_query)
        response = await query_model(model, [{"role": "user", "content": prompt}])
        return model, role, response

    results = await asyncio.gather(*(ask(m, r) for m, r in assignments))

    stage1_results = []
    for model, role, response in results:
        if response is not None:
            stage1_results.append({
                # Se incluye el rol junto al modelo para que el frontend lo
                # muestre sin cambios (sigue siendo un string).
                "model": f"{model} · {role['nombre']}",
                "role": role["nombre"],
                "role_id": role["id"],
                "base_model": model,
                "response": response.get("content", ""),
            })
    return stage1_results


# ---------------------------------------------------------------------------
# Etapa 2: meta-revisión anonimizada
# ---------------------------------------------------------------------------

async def stage2_collect_rankings(
    user_query: str,
    stage1_results: List[Dict[str, Any]],
    mode: str = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Etapa 2: cada modelo evalúa y ordena los informes anonimizados."""
    if mode is None:
        mode, user_query = await resolve_mode(user_query)
    spec = ACADEMIC_MODES[mode]

    labels = [chr(65 + i) for i in range(len(stage1_results))]  # A, B, C, ...
    label_to_model = {
        f"Response {label}": result["model"]
        for label, result in zip(labels, stage1_results)
    }

    responses_text = "\n\n".join(
        f"Response {label}:\n{result['response']}"
        for label, result in zip(labels, stage1_results)
    )

    ranking_prompt = f"""Eres parte del control de calidad de un comité académico
en modalidad **{spec['nombre']}**. Varios revisores (anónimos) emitieron informes
sobre el mismo insumo. Tu tarea es la meta-revisión: evaluar la calidad de cada
informe de revisión.

Insumo original evaluado (puede estar truncado):
{user_query[:6000]}

Informes de revisión (anonimizados):

{responses_text}

Tu tarea:
1. Evalúa cada informe según: (a) rigor y fundamentación en evidencia del
   insumo, (b) especificidad y accionabilidad de las observaciones,
   (c) cobertura de los criterios de la rúbrica, (d) justicia y tono
   profesional (crítico pero constructivo, sin sesgos).
2. Señala observaciones valiosas que solo un informe detectó, y errores o
   afirmaciones no sustentadas si los hay.
3. Al final, entrega un ranking del mejor al peor informe.

IMPORTANT: Your final ranking MUST be formatted EXACTLY as follows:
- Start with the line "FINAL RANKING:" (all caps, with colon)
- Then list the responses from best to worst as a numbered list
- Each line should be: number, period, space, then ONLY the response label (e.g., "1. Response A")
- Do not add any other text or explanations in the ranking section

Example of the correct format for the END of your response:

FINAL RANKING:
1. Response C
2. Response A
3. Response B

Now provide your evaluation and ranking:"""

    async def ask(model: str):
        response = await query_model(model, [{"role": "user", "content": ranking_prompt}])
        return model, response

    results = await asyncio.gather(*(ask(m) for m in COUNCIL_MODELS))

    stage2_results = []
    for model, response in results:
        if response is not None:
            full_text = response.get("content", "")
            stage2_results.append({
                "model": model,
                "ranking": full_text,
                "parsed_ranking": parse_ranking_from_text(full_text),
            })
    return stage2_results, label_to_model


# ---------------------------------------------------------------------------
# Etapa 3: dictamen del Presidente del Comité
# ---------------------------------------------------------------------------

async def stage3_synthesize_final(
    user_query: str,
    stage1_results: List[Dict[str, Any]],
    stage2_results: List[Dict[str, Any]],
    mode: str = None,
) -> Dict[str, Any]:
    """Etapa 3: el Presidente sintetiza el dictamen estructurado."""
    if mode is None:
        mode, user_query = await resolve_mode(user_query)
    spec = ACADEMIC_MODES[mode]

    stage1_text = "\n\n".join(
        f"Revisor ({result.get('role', 'sin rol')}, modelo {result.get('base_model', result['model'])}):\n{result['response']}"
        for result in stage1_results
    )
    stage2_text = "\n\n".join(
        f"Meta-revisión de {result['model']}:\n{result['ranking']}"
        for result in stage2_results
    )

    chairman_prompt = f"""Eres el/la Presidente/a de un Comité Académico en modalidad
**{spec['nombre']}**. Un panel de revisores con roles complementarios
(metodológico, teórico-conceptual, ética y métricas responsables, impacto y
comunicación) evaluó el insumo, y luego se realizó una meta-revisión cruzada
de la calidad de sus informes.

{COMMON_GUARDRAILS}

Insumo original:
{user_query}

ETAPA 1 — Informes de los revisores:
{stage1_text}

ETAPA 2 — Meta-revisiones (calidad de los informes):
{stage2_text}

Tu tarea como Presidente/a:
- Pondera los informes según la calidad que les atribuyó la meta-revisión.
- Consolida observaciones duplicadas y resuelve contradicciones explicando tu criterio.
- No introduzcas juicios nuevos sin base en los informes o en el insumo.

{spec['dictamen']}

Redacta el dictamen en el idioma del insumo:"""

    response = await query_model(CHAIRMAN_MODEL, [{"role": "user", "content": chairman_prompt}])

    if response is None:
        return {
            "model": CHAIRMAN_MODEL,
            "response": "Error: no fue posible generar el dictamen final.",
        }
    return {
        "model": f"{CHAIRMAN_MODEL} · Presidente del Comité ({spec['nombre']})",
        "response": response.get("content", ""),
    }


# ---------------------------------------------------------------------------
# Parsing y agregación (sin cambios funcionales respecto al original)
# ---------------------------------------------------------------------------

def parse_ranking_from_text(ranking_text: str) -> List[str]:
    """Extrae la sección FINAL RANKING del texto del modelo."""
    if "FINAL RANKING:" in ranking_text:
        parts = ranking_text.split("FINAL RANKING:")
        if len(parts) >= 2:
            ranking_section = parts[1]
            numbered_matches = re.findall(r"\d+\.\s*Response [A-Z]", ranking_section)
            if numbered_matches:
                return [re.search(r"Response [A-Z]", m).group() for m in numbered_matches]
            return re.findall(r"Response [A-Z]", ranking_section)
    return re.findall(r"Response [A-Z]", ranking_text)


def calculate_aggregate_rankings(
    stage2_results: List[Dict[str, Any]],
    label_to_model: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Promedia las posiciones de cada informe en las meta-revisiones."""
    from collections import defaultdict

    model_positions = defaultdict(list)
    for ranking in stage2_results:
        parsed_ranking = parse_ranking_from_text(ranking["ranking"])
        for position, label in enumerate(parsed_ranking, start=1):
            if label in label_to_model:
                model_positions[label_to_model[label]].append(position)

    aggregate = []
    for model, positions in model_positions.items():
        if positions:
            aggregate.append({
                "model": model,
                "average_rank": round(sum(positions) / len(positions), 2),
                "rankings_count": len(positions),
            })
    aggregate.sort(key=lambda x: x["average_rank"])
    return aggregate


# ---------------------------------------------------------------------------
# Título de conversación
# ---------------------------------------------------------------------------

async def generate_conversation_title(user_query: str) -> str:
    """Genera un título corto para la conversación."""
    title_prompt = f"""Genera un título muy corto (máximo 3-5 palabras) que resuma
la siguiente solicitud de evaluación académica. Sin comillas ni puntuación.
Responde en el idioma de la solicitud.

Solicitud: {user_query[:1500]}

Título:"""
    response = await query_model(FAST_MODEL, [{"role": "user", "content": title_prompt}], timeout=30.0)
    if response is None:
        return "Nueva evaluación"
    title = response.get("content", "Nueva evaluación").strip().strip("\"'")
    if len(title) > 50:
        title = title[:47] + "..."
    return title


# ---------------------------------------------------------------------------
# Orquestación completa
# ---------------------------------------------------------------------------

async def run_full_council(user_query: str) -> Tuple[List, List, Dict, Dict]:
    """Ejecuta el proceso completo del comité académico en 3 etapas.

    Mantiene la firma original: (stage1, stage2, stage3, metadata).
    """
    # Resolver modo una sola vez y propagarlo a todas las etapas
    mode, cleaned_query = await resolve_mode(user_query)

    stage1_results = await stage1_collect_responses(cleaned_query, mode=mode)

    if not stage1_results:
        return [], [], {
            "model": "error",
            "response": "Todos los modelos fallaron. Intenta de nuevo.",
        }, {}

    stage2_results, label_to_model = await stage2_collect_rankings(
        cleaned_query, stage1_results, mode=mode
    )

    aggregate_rankings = calculate_aggregate_rankings(stage2_results, label_to_model)

    stage3_result = await stage3_synthesize_final(
        cleaned_query, stage1_results, stage2_results, mode=mode
    )

    metadata = {
        "label_to_model": label_to_model,
        "aggregate_rankings": aggregate_rankings,
        "academic_mode": mode,
        "academic_mode_name": ACADEMIC_MODES[mode]["nombre"],
    }
    return stage1_results, stage2_results, stage3_result, metadata
