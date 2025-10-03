# Variables
DOCKER_COMPOSE = docker-compose
DOCKER = docker
PYTHON = python
DJANGO_MANAGE = $(PYTHON) manage.py

# Environment
#ENV_FILE = .env
#include $(ENV_FILE)
#export $(shell sed 's/=.*//' $(ENV_FILE))

# -------- Docker Commands --------
# Start the application with Docker
up:
	$(DOCKER_COMPOSE) up -d --build

# Stop the application
down:
	$(DOCKER_COMPOSE) down

# Show logs
logs:
	$(DOCKER_COMPOSE) logs -f

# Clean Docker (remove images, containers, volumes)
clean:
	$(DOCKER_COMPOSE) down -v
	$(DOCKER) system prune -af
	$(DOCKER) volume prune -f

# Rebuild the project
rebuild: clean up

# -------- Non-Docker (Local) Commands --------
#run virtual environment
venv:
	call venv/Scripts/activate

#create virtual environment
mkvenv:
	python -m venv venv

# Install dependencies
install:
	pip install -r requirements.txt

# Run local server
run:
	$(DJANGO_MANAGE) runserver

start:
	$(DJANGO_MANAGE) startapp

# Apply migrations
migrate:
	$(DJANGO_MANAGE) makemigrations
	$(DJANGO_MANAGE) migrate

# Make migrations

migrations:
	$(DJANGO_MANAGE) makemigrations
	$(DJANGO_MANAGE) migrate

# Create superuser
superuser:
	$(DJANGO_MANAGE) createsuperuser

# Collect static files
static:
	$(DJANGO_MANAGE) collectstatic --noinput

# Run Django shell
shell:
	$(DJANGO_MANAGE) shell

# Run tests
test:
	$(DJANGO_MANAGE) test

# Format code using black & isort
format:
	black .
	isort .

# Default command
.PHONY: up down logs clean rebuild venv install run migrate makemigrations superuser collectstatic shell test format
