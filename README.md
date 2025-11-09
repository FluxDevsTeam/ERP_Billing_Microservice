# ERP Billing Microservice

This is the billing microservice for the ERP SAAS application. It handles plans, subscriptions, payments, and customer management.

## Features

*   **Plan Management**: Create, read, update, and delete subscription plans.
*   **Subscription Management**: Handle user subscriptions, including creation, renewal, suspension, and plan changes.
*   **Trial Management**: Support for trial periods for new users.
*   **Customer Portal**: A dedicated portal for customers to manage their subscriptions.
*   **Auto-renewal**: Automatic subscription renewals.
*   **Access Control**: Middleware to check for active subscriptions and grant or deny access to ERP features.
*   **Payment Processing**: Integration with payment gateways to handle transactions.
*   **Health Checks**: Endpoints to monitor the health of the microservice.
*   **API Documentation**: Auto-generated API documentation using Swagger (drf-yasg).

## Requirements

The project dependencies are listed in the `requirements.txt` file. Key dependencies include:

*   Django
*   Django REST Framework
*   Celery
*   Redis
*   djangorestframework-simplejwt
*   drf-yasg

## Installation

1.  **Clone the repository:**

    ```bash
    git clone https://github.com/FluxDevsTeam/ERP_Billing_Microservice.git
    cd ERP_Billing_Microservice
    ```

2.  **Create a virtual environment:**

    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
    ```

3.  **Install dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

4.  **Set up environment variables:**

    Create a `.env` file in the root directory and add the necessary configuration.

5.  **Run database migrations:**

    ```bash
    python manage.py migrate
    ```

6.  **Create a superuser:**

    ```bash
    python manage.py createsuperuser
    ```

## Usage

To run the development server, use the following command:

```bash
python manage.py runserver
```

The application will be available at `http://127.0.0.1:8000`.

## API Documentation

API documentation is available at the following endpoints:

*   **Swagger UI**: `/api/docs/`
*   **ReDoc**: `/api/redoc/`

## Testing

To run the test suite, use the following command:

```bash
python manage.py test
```

## Makefile Commands

A `Makefile` is provided for convenience. Here are some of the available commands:

*   `make install`: Install dependencies.
*   `make migrate`: Apply database migrations.
*   `make run`: Run the development server.
*   `make test`: Run tests.
*   `make superuser`: Create a superuser.
*   `make format`: Format the code using black and isort.

## Contributing

Contributions are welcome! Please feel free to submit a pull request.

1.  Fork the Project
2.  Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3.  Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4.  Push to the Branch (`git push origin feature/AmazingFeature`)
5.  Open a Pull Request

## License

This project is licensed under the MIT License - see the `LICENSE` file for details.
