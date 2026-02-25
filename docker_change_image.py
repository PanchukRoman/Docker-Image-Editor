#!/usr/bin/env python3
"""
Docker Image Editor — терминальный интерфейс для копирования файлов в/из контейнера
и создания нового образа на основе изменений.
"""

import docker
import subprocess
import os
import sys
from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

console = Console()
client = docker.from_env()

def run_command(cmd):
    """Выполняет команду и возвращает успех/неудачу."""
    result = subprocess.run(cmd, shell=True)
    return result.returncode == 0

def get_container_files(container_id, path):
    """Получает список файлов в директории контейнера через docker exec ls."""
    try:
        cmd = f"docker exec {container_id} ls -1 {path}"
        output = subprocess.check_output(cmd, shell=True, text=True).strip()
        if not output:
            return []
        return output.split('\n')
    except subprocess.CalledProcessError:
        return None

def show_local_images():
    """Показывает список локальных Docker образов в виде таблицы."""
    try:
        images = client.images.list()
        if not images:
            console.print("[yellow]Нет локальных образов.[/yellow]")
            return
        table = Table(title="Локальные Docker образы")
        table.add_column("Репозиторий", style="cyan")
        table.add_column("Тег", style="green")
        table.add_column("Размер", style="white")
        table.add_column("ID", style="dim")
        for img in images:
            if img.tags:
                for tag in img.tags:
                    repo, tag_name = tag.split(':', 1) if ':' in tag else (tag, 'latest')
                    size = img.attrs['Size']
                    size_str = f"{size / (1024*1024):.1f} MB" if size else "N/A"
                    table.add_row(repo, tag_name, size_str, img.short_id)
            else:
                size = img.attrs['Size']
                size_str = f"{size / (1024*1024):.1f} MB" if size else "N/A"
                table.add_row("<none>", "<none>", size_str, img.short_id)
        console.print(table)
    except Exception as e:
        console.print(f"[red]Ошибка при получении списка образов: {e}[/red]")

def docker_run_image(name):
    """Запускает контейнер из образа с sleep, возвращает ID контейнера."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console
    ) as progress:
        progress.add_task(description="Проверка образа...", total=None)
        try:
            client.images.get(name)
            console.print(f"[green]✓[/green] Образ {name} существует.")
        except docker.errors.ImageNotFound:
            console.print(f"[yellow]Образ {name} не найден. Выполняется pull...[/yellow]")
            client.images.pull(name)
        progress.add_task(description="Запуск контейнера...", total=None)
        container = client.containers.run(name, entrypoint="sleep", command='9999999', detach=True)
    console.print(f"[green]✓[/green] Контейнер запущен, [bold cyan]ID: {container.id}[/bold cyan]")
    return container.id

def stop_and_remove_container(container_id):
    """Останавливает и удаляет контейнер."""
    try:
        container = client.containers.get(container_id)
        container.stop()
        container.remove()
        console.print(f"[green]✓[/green] Контейнер {container_id} остановлен и удалён.")
    except docker.errors.NotFound:
        console.print(f"[red]✗[/red] Контейнер {container_id} не найден.")
    except Exception as e:
        console.print(f"[red]✗ Ошибка при удалении контейнера: {e}[/red]")

def choose_files_to_copy_from_container(container_id):
    """Позволяет пользователю выбрать несколько файлов из контейнера."""
    path = questionary.text(
        "Введите путь к директории в контейнере (например, /app):",
        default="/"
    ).ask()
    if not path:
        return

    files = get_container_files(container_id, path)
    if files is None:
        console.print("[red]Не удалось получить список файлов. Проверьте путь.[/red]")
        return

    if not files:
        console.print("[yellow]В этой директории нет файлов.[/yellow]")
        return

    # Показываем предпросмотр файлов
    table = Table(title=f"Файлы в {path}")
    table.add_column("Имя файла", style="cyan")
    for f in files[:10]:
        table.add_row(f)
    if len(files) > 10:
        table.add_row("... и ещё", str(len(files)-10))
    console.print(table)

    # Выбор нескольких файлов
    selected = questionary.checkbox(
        "Выберите файлы для копирования:",
        choices=files
    ).ask()

    if not selected:
        console.print("[yellow]Ничего не выбрано.[/yellow]")
        return

    dest_dir = questionary.path(
        "Введите целевую директорию на локальной машине (по умолчанию /tmp):",
        default="/tmp"
    ).ask()
    if not dest_dir:
        dest_dir = "/tmp"

    os.makedirs(dest_dir, exist_ok=True)

    for file in selected:
        src = f"{container_id}:{path}/{file}"
        dst = os.path.join(dest_dir, file)
        cmd = f"docker cp {src} {dst}"
        with console.status(f"[bold green]Копируется {file}..."):
            if run_command(cmd):
                console.print(f"  [green]✓[/green] {file} -> {dst}")
            else:
                console.print(f"  [red]✗[/red] Ошибка при копировании {file}")

def choose_files_to_copy_to_container(container_id):
    """Позволяет выбрать локальные файлы и скопировать их в контейнер."""
    local_files = questionary.path(
        "Введите путь к локальному файлу или директории (если директория, будут скопированы все файлы внутри):"
    ).ask()
    if not local_files:
        return

    path = Path(local_files)
    if path.is_dir():
        files_to_copy = list(path.glob('*'))
        if not files_to_copy:
            console.print("[yellow]Директория пуста.[/yellow]")
            return
        choices = [str(f) for f in files_to_copy]
    else:
        if not path.exists():
            console.print("[red]Файл не найден.[/red]")
            return
        choices = [str(path)]

    if len(choices) > 1:
        selected = questionary.checkbox(
            "Выберите файлы для копирования:",
            choices=choices
        ).ask()
    else:
        selected = choices

    if not selected:
        console.print("[yellow]Ничего не выбрано.[/yellow]")
        return

    target_dir = questionary.text(
        "Введите целевую директорию в контейнере (например, /app):"
    ).ask()
    if not target_dir:
        return

    for local_path in selected:
        filename = os.path.basename(local_path)
        cmd = f"docker cp {local_path} {container_id}:{target_dir}/"
        with console.status(f"[bold green]Копируется {filename}..."):
            if run_command(cmd):
                console.print(f"  [green]✓[/green] {filename} скопирован в {target_dir}")
            else:
                console.print(f"  [red]✗[/red] Ошибка при копировании {filename}")

def commit_container(container_id):
    """Создаёт новый образ из контейнера."""
    new_tag = questionary.text("Введите новое имя и тег для образа (например, myimage:latest):").ask()
    if not new_tag:
        return
    try:
        container = client.containers.get(container_id)
        if ':' in new_tag:
            repo, tag = new_tag.split(':', 1)
        else:
            repo, tag = new_tag, 'latest'
        container.commit(repository=repo, tag=tag)
        console.print(f"[green]✓[/green] Образ [bold]{new_tag}[/bold] создан.")
    except Exception as e:
        console.print(f"[red]✗ Ошибка при коммите: {e}[/red]")

def main():
    console.print(Panel.fit("🐳 [bold cyan]Docker Image Editor[/bold cyan] 🐳", border_style="cyan"))

    action = questionary.select(
        "Что вы хотите сделать?",
        choices=[
            "Сохранить файл(ы) из образа на локальную машину",
            "Добавить файл(ы) в образ и создать новый образ",
            "Выйти"
        ]
    ).ask()

    if action == "Выйти" or not action:
        return

    # Показываем список локальных образов
    show_local_images()

    image_name = questionary.text("Введите имя образа (например, ubuntu:latest):").ask()
    if not image_name:
        return

    container_id = docker_run_image(image_name)

    try:
        if action.startswith("Сохранить"):
            while True:
                choose_files_to_copy_from_container(container_id)
                if not questionary.confirm("Хотите скопировать ещё файлы?").ask():
                    break
        else:  # Добавить файлы
            while True:
                choose_files_to_copy_to_container(container_id)
                if not questionary.confirm("Хотите добавить ещё файлы?").ask():
                    break
            if questionary.confirm("Создать новый образ из изменённого контейнера?").ask():
                commit_container(container_id)
    finally:
        if questionary.confirm("Удалить контейнер?").ask():
            stop_and_remove_container(container_id)
        else:
            console.print(f"[yellow]Контейнер {container_id} остаётся запущенным. Не забудьте остановить его вручную.[/yellow]")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Прервано пользователем.[/yellow]")
        sys.exit(0)
