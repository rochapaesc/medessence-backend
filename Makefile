DJANGO_CONTAINER = medessence_django

prebuild:
	cp example.env .env
	cp .db.env.example .db.env

migrate:
	docker compose exec $(DJANGO_CONTAINER) python manage.py migrate

zero_migrate:
	docker compose exec $(DJANGO_CONTAINER) python manage.py migrate accounts zero
	docker compose exec $(DJANGO_CONTAINER) python manage.py migrate core zero
	docker compose exec $(DJANGO_CONTAINER) python manage.py migrate tenants zero

makemigrations:
	docker compose exec $(DJANGO_CONTAINER) python manage.py makemigrations

reset_migrations:
	docker compose exec $(DJANGO_CONTAINER) find . -path "*/migrations/*.py" -not -name "__init__.py" -not -path "*/venv/*" -delete
	docker compose exec $(DJANGO_CONTAINER) python manage.py makemigrations

load_data:
	docker compose exec $(DJANGO_CONTAINER) python manage.py loaddata fixtures/*.json

lint:
	ruff check . --fix
	ruff format .

lint-check:
	ruff check .
	ruff format . --check

test:
	env/bin/python -m pytest

test-docker:
	docker compose exec $(DJANGO_CONTAINER) pytest

seed:
	docker compose exec $(DJANGO_CONTAINER) python manage.py seed

runserver:
	docker compose up

build:
	docker compose down
	docker compose up --build -d

delete_pycache:
	find . -path "*/__pycache__" | xargs rm -rf

update:
	git add . && git commit -m "$(commit)" && git push
	ssh -i /home/medessence/.ssh/datastone root@68.183.125.100 'cd /home/medessence-backend/ && git pull origin main'
