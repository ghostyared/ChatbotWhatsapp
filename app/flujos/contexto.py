from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Contexto:
    """Todo lo que un flujo necesita saber del mensaje entrante."""
    tel: str
    usu: object
    nombre: str
    txt: str = ""          # texto original (sin normalizar)
    btn_id: str = ""       # id de botón / fila de lista pulsada
    tipo: str = "text"     # text | interactive | image | document | ...
    media: Optional[dict] = field(default=None)   # {'id','mime','filename','caption'}
