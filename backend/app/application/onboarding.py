from app.application.errors import ApplicationError
from app.application.ports import OnboardingRepository, SecretStorePort, SettingsRepository
from app.domain.models import ModelId, OnboardingProgress, UserProfile


class OnboardingService:
    def __init__(
        self,
        repository: OnboardingRepository,
        settings_repository: SettingsRepository,
        secret_store: SecretStorePort,
    ) -> None:
        self._repository = repository
        self._settings_repository = settings_repository
        self._secret_store = secret_store

    async def get(self) -> OnboardingProgress:
        return await self._repository.get_onboarding_progress()

    async def complete_intro(self) -> OnboardingProgress:
        return await self._repository.advance_onboarding_intro()

    async def save_profile(
        self, display_name: str, email: str
    ) -> tuple[UserProfile, OnboardingProgress]:
        progress = await self.get()
        if progress.current_step == "intro":
            raise ApplicationError(
                "onboarding_step_out_of_order",
                "Complete the introduction before saving your profile.",
            )
        normalized_name = display_name.strip()
        if not normalized_name:
            raise ApplicationError("invalid_profile", "Name cannot be empty.")
        return await self._repository.save_onboarding_profile(normalized_name, email)

    async def save_model(self, model_id: ModelId) -> OnboardingProgress:
        progress = await self.get()
        selected_model_id = await self._settings_repository.get_selected_model_id()
        if progress.current_step == "complete":
            if selected_model_id == model_id:
                return progress
            raise ApplicationError(
                "onboarding_already_complete",
                "Onboarding is already complete. Choose another model in Settings.",
            )
        if progress.current_step != "model":
            raise ApplicationError(
                "onboarding_step_out_of_order",
                "Complete your profile before choosing a model.",
            )

        model = next(
            (
                candidate
                for candidate in await self._settings_repository.list_models()
                if candidate.id == model_id
            ),
            None,
        )
        if model is None:
            raise ApplicationError("model_not_available", "The selected model is not available.")

        if model.requires_api_key:
            configured, _ = await self._secret_store.status(model.provider_id)
            if not configured:
                raise ApplicationError(
                    "provider_not_configured",
                    f"{model.provider_name} is unavailable. Contact the Trellis operator.",
                )
        if not await self._repository.complete_onboarding(model_id):
            raise ApplicationError(
                "onboarding_step_out_of_order",
                "The selected model could not be saved for onboarding.",
            )
        return await self.get()
