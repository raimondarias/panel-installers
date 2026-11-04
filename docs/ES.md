# Pterodactyl Installer · Guía rápida en español

Instala desde el repositorio publicado:

```bash
curl -fsSL https://raw.githubusercontent.com/raimondarias/pterodactyl-installer/main/install.sh -o ptero-setup.sh && sudo bash ptero-setup.sh
```

La primera pregunta estará en inglés: **Which language would you like to use?**. Después se descarga únicamente el paquete del idioma elegido, se verifica y comienza el asistente sin repetir esa pregunta. El repositorio publicado incluye los tres paquetes de idioma.

Al comenzar verás **1. English · 2. Español · 3. Português (Brasil)**. Selecciona 2 para usar todo el asistente en español. El idioma también se puede cambiar desde el menú.

Selecciona **Simple (Recomendado)** o **Avanzado**, y después:

- **Standalone nativo (recomendado), primero**, o Docker.
- MariaDB (recomendado) o MySQL.
- IP pública IPv4 de la máquina, con HTTP y sin SSL; o dominio/subdominio, con HTTPS recomendado.
- Correo, nombre de usuario y contraseña oculta del primer administrador. Si dejas la contraseña vacía se genera una y se muestra para que la guardes.
- SMTP opcional, configurable más tarde.

El modo avanzado también permite elegir versión, PHP 8.3/8.4 en standalone, puerto HTTP, IP de escucha y zona horaria. Las decisiones se muestran antes de instalar.

El motor se elige una sola vez para Panel y juegos. El asistente también ofrece preparar un host SQL local aislado, nativo (recomendado) o en Docker, registrar uno existente o dejarlo para después (por defecto). La opción nativa separa usuario del sistema, datos, puerto y servicio de la base del Panel. Docker sigue siendo necesario para los juegos y su red privada. El host se registra automáticamente en el Panel y entrega host, puerto, usuario y contraseña en pantalla y en un archivo privado. Avanzado permite personalizar las credenciales y asociar un nodo existente; 0 lo deja global. Cada servidor de juego necesita un límite de bases de datos mayor que cero.

Los menús 20 y 21 configuran el host SQL y muestran sus credenciales. El idioma cambia en la opción 23. Las bases de juegos y sus credenciales se conservan al purgar el Panel. Puedes preparar SQL privado en otra máquina con la acción remota por SSH; las rutas privadas y claves verificadas deben estar listas. El instalador prepara y registra el host, pero las copias y restauraciones de los datos de juegos corresponden a Pterodactyl o a tu sistema de copias. Consulta los detalles de redes y firewall en [la guía principal](OPERATIONS.md#game-database-hosts).

Vista previa sin root ni instalación:

```bash
bash scripts/run_local.sh --language es --preview
```

Se aceptan Ubuntu 22.04/24.04/26.04 y Debian 12/13 con systemd. Se necesitan root, Python 3.10+, al menos 4 GiB libres y memoria suficiente. El arranque instala Python si falta. No se necesitan paquetes de pip. MySQL nativo en Debian requiere amd64; en Debian ARM64 usa MariaDB o Docker.

Wings necesita Docker para ejecutar juegos, aunque el Panel sea standalone. Puedes usar Wings como servicio systemd (recomendado) o como contenedor oficial. Usa la creación automática de ubicación, nodo y allocations, o créalos en el Panel y vincula Wings con el token Auto-Deploy. El token se introduce oculto y no pasa por argumentos de proceso.

El menú permite instalar/desinstalar ambos componentes juntos o separados, actualizar, emitir SSL para Wings, hacer copias, restaurar, consultar diagnósticos/registros y configurar correo/administradores. Cada actualización del Panel guarda un punto de recuperación verificado: si falla, el instalador ofrece revertir de inmediato, y **Revertir actualización del Panel** (35) vuelve a la versión anterior con su base de datos compatible en cualquier momento. Además comprueba la compatibilidad entre Panel y Wings antes de instalar, actualizar o revertir, conserva automáticamente las 5 copias locales más recientes de cada tipo (opción 36) y, si vuelves a ejecutar un `ptero-setup.sh` guardado, se actualiza solo a la última versión verificada.

Al desinstalar se conservan los datos por defecto. Borrarlos requiere confirmación y una copia correcta. Las copias del Panel incluyen su propia base SQL y las claves originales. Las únicas copias ofrecidas son **Panel y configuración de Wings**; nunca incluyen archivos, volúmenes, mundos ni bases de datos de los servidores alojados. Guarda los archivos `.tar.gz` y `.sha256` fuera del servidor.

Panel v2 es beta. Los tests automatizados y las comprobaciones reales de archivos/configuraciones no sustituyen probar un despliegue completo en un VPS limpio. Consulta [el informe](../test-results/REPORT.md), [la guía principal](../README.md) y [las pruebas de integración](TESTING.md).

La interfaz abre una TUI de pantalla completa en una terminal SSH compatible. Usa ↑/↓ para moverte, Tab o ←/→ para cambiar de grupo, Enter para elegir, / para buscar, F5 para actualizar estados y Esc/Q para volver o salir. Las operaciones están implementadas. Las acciones que requieren un componente aún no instalado explican qué falta y cómo prepararlo antes de ejecutarse. En una máquina nueva, Diagnóstico muestra información útil y el siguiente paso. Prueba sin instalar con `bash ptero-setup.sh --preview`; usa `--plain` para menú numérico y `--no-animation` para reducir movimiento. [Detalles y capturas](TUI.md).

Puedes seleccionar cada opción con un clic del ratón en terminales compatibles, incluido el selector inicial de idioma. También puedes pulsar las flechas y Enter.

Incluye creación automática de nodos y allocations, SQL de juegos nativo aislado, copias cifradas programadas del Panel y/o configuración de Wings, restauración del Panel, reanudación por etapas y preparación remota por SSH con claves verificadas. Las opciones 28, 29 y 30 programan esas copias, ejecutan la programación y descargan una copia verificada. Una máquina con solo Wings también puede programar la copia de su configuración. Consulta [las operaciones avanzadas](ADVANCED.md).

Ubuntu 26.04 se prueba con Docker. Standalone requiere un repositorio PHP 8.3/8.4 preparado; el PPA probado aún no publica resolute. Sin ese requisito, el instalador se detiene antes de cambiar paquetes.
