import os
import re
import json
import httpx
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from base58 import b58decode, b58encode
from cryptography.fernet import Fernet
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.transaction import Transaction
from telethon import TelegramClient, events
from telegram import Bot, Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
)
from telethon.sessions import StringSession

# Загрузка конфигурации
load_dotenv()

# Состояния
ADD_CHANNEL, IMPORT_WALLET, SET_AMOUNT, SET_SLIPPAGE, REMOVE_CHANNEL, REMOVE_WALLET = range(6)


class UserBotManager:
    def __init__(self):
        self.client = None
        self.session_file = Path(__file__).parent / 'userbot.session'
        self.phone = os.getenv("USERBOT_PHONE")
        self.api_id = int(os.getenv("TELEGRAM_API_ID"))
        self.api_hash = os.getenv("TELEGRAM_API_HASH")
        self.password = os.getenv("USERBOT_PASSWORD")  # Если есть 2FA

    async def initialize(self):
        """Инициализация userbot из сохраненной сессии или создание новой"""
        if self.session_file.exists():
            with open(self.session_file, 'r') as f:
                session_string = f.read().strip()
                self.client = TelegramClient(
                    StringSession(session_string),
                    self.api_id,
                    self.api_hash
                )
        else:
            self.client = TelegramClient(
                StringSession(),
                self.api_id,
                self.api_hash
            )

        try:
            await self._connect()
            return True
        except Exception as e:
            print(f"Ошибка инициализации UserBot: {e}")
            return False

    async def _connect(self):
        """Подключение userbot с обработкой всех сценариев"""
        if not self.phone:
            raise ValueError("USERBOT_PHONE не установлен в .env")

        await self.client.connect()

        if not await self.client.is_user_authorized():
            print("Авторизация UserBot...")
            await self.client.send_code_request(self.phone)

            # Если есть пароль для 2FA
            if self.password:
                await self.client.sign_in(
                    phone=self.phone,
                    code=input('Введите код из Telegram: '),
                    password=self.password
                )
            else:
                await self.client.sign_in(
                    phone=self.phone,
                    code=input('Введите код из Telegram: ')
                )

            # Сохраняем сессию
            with open(self.session_file, 'w') as f:
                f.write(self.client.session.save())

        print("UserBot успешно авторизован")

    async def start_monitoring(self, channels, callback):
        """Запуск мониторинга каналов"""
        if not self.client:
            raise RuntimeError("UserBot не инициализирован")

        @self.client.on(events.NewMessage(chats=channels))
        async def handler(event):
            await callback(event)

        print(f"🟢 UserBot начал мониторинг {len(channels)} каналов...")
        await self.client.run_until_disconnected()

    async def stop(self):
        """Остановка userbot"""
        if self.client and self.client.is_connected():
            await self.client.disconnect()


class SecureStorage:
    def __init__(self):

        key = os.getenv("ENCRYPTION_KEY")
        print(f"Raw key: {repr(key)}")
        print(f"Length: {len(key)}")
        print(f"Stripped: {repr(re.sub(r'[^a-zA-Z0-9-_]', '', key))}")
        print(f"Stripped length: {len(re.sub(r'[^a-zA-Z0-9-_]', '', key))}")
        if not key:
            raise ValueError("ENCRYPTION_KEY не найден в .env")

        # Удаляем возможные пробелы и лишние символы

        key = key.strip()

        # Проверка длины
        if len(key) != 44:
            raise ValueError(f"Неправильная длина ключа. Нужно 44 символа, получено {len(key)}")
        try:
            self.cipher = Fernet(key.encode('utf-8'))
        except Exception as e:
            raise ValueError(f"Ошибка инициализации Fernet: {str(e)}")

        self.data_file = Path(__file__).parent / 'encrypted_data.json'
        self.data = {
            "wallets": {},  # Формат: {"user_id": {"private_key": "...", "amount": 100000000, "slippage": 0.5}}
            "channels": {},
            "last_processed_ids": {}
        }
        self._load_data()

    def _load_data(self):
        try:
            if self.data_file.exists():
                with open(self.data_file, 'r') as f:
                    self.data = json.load(f)
        except Exception as e:
            print(f"Ошибка загрузки данных: {e}")

    def save_data(self):
        try:
            with open(self.data_file, 'w') as f:
                json.dump(self.data, f)
        except Exception as e:
            print(f"Ошибка сохранения данных: {e}")


class SolanaTrader:
    def __init__(self):
        self.client = AsyncClient(os.getenv("SOLANA_RPC"))
        self.gmgn_api = os.getenv("GMGN_API")

    async def find_tokens(self, text: str):
        """Поиск токенов в тексте сообщения"""
        return re.findall(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b', text)

    async def buy_token(self, private_key: str, token: str, amount: int, slippage: float):
        """Покупка токена через GMGN API"""
        try:
            secret_key = b58decode(private_key)
            keypair = Keypair.from_bytes(secret_key)

            # 1. Получаем маршрут обмена с пользовательскими параметрами
            route = await self._get_swap_route(
                wallet_pubkey=keypair.pubkey(),
                token=token,
                amount=amount,
                slippage=slippage
            )
            # 2. Подписываем транзакцию
            tx = Transaction.deserialize(b58decode(route["raw_tx"]))
            tx.sign([keypair])  # Подписываем транзакцию
            signed_tx = b58encode(tx.serialize()).decode()

            # 3. Отправляем транзакцию
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.gmgn_api}/tx/send_transaction",
                    json={"signedTx": signed_tx}
                )
                return response.json()
        except Exception as e:
            print(f"Ошибка при покупке токена: {e}")
            raise

    async def _get_swap_route(self, wallet_pubkey, token, amount, slippage):
        params = {
            "token_in": "So11111111111111111111111111111111111111112",
            "token_out": token,
            "in_amount": str(amount),
            "from_address": str(wallet_pubkey),
            "slippage": str(slippage)  # Используем пользовательское проскальзывание
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.gmgn_api}/tx/get_swap_route",
                params=params
            )
            return response.json()


class TokenHunterBot:
    def __init__(self):
        load_dotenv()
        self.bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
        self.storage = SecureStorage()
        self.trader = SolanaTrader()
        self.userbot = UserBotManager()  # Менеджер userbot

        # Настройка обработчиков команд
        self.app = Application.builder().token(os.getenv("TELEGRAM_TOKEN")).build()
        self._setup_handlers()


    async def _start_background_tasks(self):
        """Запуск фоновых задач"""
        if not self.storage.data["channels"]:
            print("ℹ️ Нет каналов для мониторинга")
            return

        # Инициализация userbot в фоне
        asyncio.create_task(self._init_userbot())


    async def _init_userbot(self):
        """Инициализация userbot и запуск мониторинга"""
        try:
            success = await self.userbot.initialize()
            if not success:
                print("❌ Не удалось инициализировать UserBot")
                return

            channels = list(self.storage.data["channels"].keys())
            await self.userbot.start_monitoring(
                channels=channels,
                callback=self._handle_channel_message
            )
        except Exception as e:
            print(f"❌ Ошибка мониторинга через UserBot: {e}")


    async def _handle_channel_message(self, event):
        """Обработка сообщений из каналов"""
        try:
            tokens = await self.trader.find_tokens(event.raw_text)
            for token in tokens:
                await self._process_token(event, token)
        except Exception as e:
            print(f"Ошибка обработки сообщения: {e}")


    async def run(self):
        """Запуск бота"""
        try:
            await self.app.initialize()
            await self.app.start()
            await self.app.updater.start_polling()

            # Запуск фоновых задач
            await self._start_background_tasks()

            print("🟢 Бот запущен и начал работу")
            while True:
                await asyncio.sleep(3600)
        except Exception as e:
            print(f"🔴 Ошибка запуска бота: {e}")
            await self.app.stop()
            await self.userbot.stop()
            raise

    def _get_main_keyboard(self):
        """Главное меню с кнопками"""
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("➕ Добавить канал"), KeyboardButton("➖ Удалить канал")],
                [KeyboardButton("💼 Импорт кошелька"), KeyboardButton("🗑️ Удалить кошелек")],
                [KeyboardButton("⚙️ Настройки покупки"), KeyboardButton("📋 Мои каналы")],
                [KeyboardButton("❓ Помощь")]
            ],
            resize_keyboard=True,
            one_time_keyboard=False
        )

    def _get_settings_keyboard(self):
        """Клавиатура настроек"""
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("💰 Установить сумму"), KeyboardButton("📉 Установить slippage")],
                [KeyboardButton("🔙 На главную")]
            ],
            resize_keyboard=True
        )

    def _get_back_keyboard(self):
        """Кнопка возврата"""
        return ReplyKeyboardMarkup(
            [[KeyboardButton("🔙 Отмена")]],
            resize_keyboard=True
        )

    def _get_wallet_remove_keyboard(self):
        """Клавиатура подтверждения удаления кошелька"""
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("✅ Да, удалить"), KeyboardButton("❌ Нет, отменить")],
            ],
            resize_keyboard=True
        )


    def _setup_handlers(self):
        # Обновляем обработчики
        button_handlers = [
            MessageHandler(filters.Text(["❓ Помощь"]), self._cmd_start),
            MessageHandler(filters.Text(["➕ Добавить канал"]), self._cmd_add_channel),
            MessageHandler(filters.Text(["➖ Удалить канал"]), self._cmd_remove_channel),
            MessageHandler(filters.Text(["💼 Импорт кошелька"]), self._cmd_import_wallet),
            MessageHandler(filters.Text(["⚙️ Настройки покупки"]), self._show_settings),
            MessageHandler(filters.Text(["📋 Мои каналы"]), self._cmd_list_channels),
            MessageHandler(filters.Text(["💰 Установить сумму"]), self._cmd_set_amount),
            MessageHandler(filters.Text(["📉 Установить slippage"]), self._cmd_set_slippage),
            MessageHandler(filters.Text(["🔙 На главную"]), self._back_to_main),
            MessageHandler(filters.Text(["🔙 Отмена"]), self._back_to_main),
            MessageHandler(filters.Text(["🗑️ Удалить кошелек"]), self._cmd_remove_wallet),
            MessageHandler(filters.Text(["✅ Да, удалить"]), self._confirm_remove_wallet),
            MessageHandler(filters.Text(["❌ Нет, отменить"]), self._back_to_main),
        ]

        # Обновленный ConversationHandler
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("start", self._cmd_start),
                MessageHandler(filters.Text(["➕ Добавить канал"]), self._cmd_add_channel),
                MessageHandler(filters.Text(["➖ Удалить канал"]), self._cmd_remove_channel),
                MessageHandler(filters.Text(["💼 Импорт кошелька"]), self._cmd_import_wallet),
                MessageHandler(filters.Text(["💰 Установить сумму"]), self._cmd_set_amount),
                MessageHandler(filters.Text(["📉 Установить slippage"]), self._cmd_set_slippage),
                MessageHandler(filters.Text(["🗑️ Удалить кошелек"]), self._cmd_remove_wallet),
            ],
            states={
                ADD_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_add_channel)],
                REMOVE_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_remove_channel)],
                IMPORT_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_import_wallet)],
                SET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_set_amount)],
                SET_SLIPPAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_set_slippage)],
                REMOVE_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_remove_wallet)],
            },
            fallbacks=[
                MessageHandler(filters.Text(["🔙 На главную", "🔙 Отмена"]), self._back_to_main),
            ],
            allow_reentry=True
        )

        self.app.add_handler(conv_handler)
        for handler in button_handlers:
            self.app.add_handler(handler)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start и кнопки Помощь"""
        await update.message.reply_text(
            "🛠 Sa_Finder_Bot\n\n"
            "Автоматически покупает новые токены из отслеживаемых каналов\n\n"
            "Используйте кнопки ниже для управления:",
            reply_markup=self._get_main_keyboard()
        )
        return ConversationHandler.END

    async def _cmd_add_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Запрос на добавление каналов"""
        await update.message.reply_text(
            "Отправьте @username каналов через запятую:\n"
            "Пример: @channel1, @channel2, @channel3\n\n"
            "Или нажмите '🔙 Отмена'",
            reply_markup=self._get_back_keyboard()
        )
        return ADD_CHANNEL

    async def _handle_add_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка добавления одного или нескольких каналов"""
        user_id = str(update.message.from_user.id)
        text = update.message.text.strip()

        # Обработка отмены
        if text in ["🔙 Назад", "🔙 На главную", "🔙 Отмена"]:
            await self._back_to_main(update, context)
            return ConversationHandler.END

        # Разделяем ввод по запятым и очищаем от пробелов
        channels = [ch.strip() for ch in text.split(',')]
        added_channels = []
        already_added = []
        invalid_channels = []

        for channel in channels:
            if not channel.startswith("@"):
                invalid_channels.append(channel)
                continue

            if channel not in self.storage.data["channels"]:
                self.storage.data["channels"][channel] = []

            if user_id not in self.storage.data["channels"][channel]:
                self.storage.data["channels"][channel].append(user_id)
                added_channels.append(channel)
            else:
                already_added.append(channel)

        # Сохраняем изменения, если были добавлены новые каналы
        if added_channels:
            self.storage.save_data()

        # Формируем ответ
        response = []
        if added_channels:
            response.append(f"✅ Добавлены каналы: {', '.join(added_channels)}")
        if already_added:
            response.append(f"ℹ️ Уже были добавлены: {', '.join(already_added)}")
        if invalid_channels:
            response.append(f"❌ Неверный формат: {', '.join(invalid_channels)}\n(должно начинаться с @)")

        if not added_channels and not already_added and invalid_channels:
            # Если все каналы невалидные, просим повторить ввод
            await update.message.reply_text(
                "\n".join(response),
                reply_markup=self._get_back_keyboard()
            )
            return ADD_CHANNEL
        else:
            await update.message.reply_text(
                "\n".join(response),
                reply_markup=self._get_main_keyboard()
            )
            return ConversationHandler.END

    async def _cmd_remove_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Запрос на удаление каналов"""
        user_id = str(update.message.from_user.id)
        user_channels = [
            channel for channel, users in self.storage.data["channels"].items()
            if user_id in users
        ]

        if not user_channels:
            await update.message.reply_text(
                "ℹ️ У вас нет отслеживаемых каналов",
                reply_markup=self._get_main_keyboard()
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "Отправьте @username каналов для удаления через запятую:\n"
            f"Ваши каналы: {', '.join(user_channels)}\n\n"
            "Или нажмите '🔙 Отмена'",
            reply_markup=self._get_back_keyboard()
        )
        return REMOVE_CHANNEL

    async def _handle_remove_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка удаления одного или нескольких каналов"""
        user_id = str(update.message.from_user.id)
        text = update.message.text.strip()

        # Обработка отмены
        if text in ["🔙 Назад", "🔙 На главную", "🔙 Отмена"]:
            await self._back_to_main(update, context)
            return ConversationHandler.END

        # Разделяем ввод по запятым и очищаем от пробелов
        channels = [ch.strip() for ch in text.split(',')]
        removed_channels = []
        not_found = []
        not_subscribed = []

        for channel in channels:
            if channel not in self.storage.data["channels"]:
                not_found.append(channel)
                continue

            if user_id in self.storage.data["channels"][channel]:
                self.storage.data["channels"][channel].remove(user_id)
                removed_channels.append(channel)

                # Удаляем канал полностью, если подписчиков больше нет
                if not self.storage.data["channels"][channel]:
                    del self.storage.data["channels"][channel]
            else:
                not_subscribed.append(channel)

        # Сохраняем изменения, если были удаления
        if removed_channels:
            self.storage.save_data()

        # Формируем ответ
        response = []
        if removed_channels:
            response.append(f"✅ Удалены каналы: {', '.join(removed_channels)}")
        if not_subscribed:
            response.append(f"ℹ️ Вы не подписаны: {', '.join(not_subscribed)}")
        if not_found:
            response.append(f"❌ Не найдены: {', '.join(not_found)}")

        await update.message.reply_text(
            "\n".join(response),
            reply_markup=self._get_main_keyboard()
        )
        return ConversationHandler.END

    async def _cmd_import_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Импорт кошелька"""
        await update.message.reply_text(
            "Отправьте приватный ключ в формате Base58:",
            reply_markup=self._get_back_keyboard()
        )
        return IMPORT_WALLET

    async def _handle_import_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка импорта кошелька"""
        user_id = str(update.message.from_user.id)
        text = update.message.text.strip()

        try:
            secret_key = b58decode(text)
            keypair = Keypair.from_bytes(secret_key)

            # Инициализация кошелька с настройками по умолчанию
            self.storage.data["wallets"][user_id] = {
                "private_key": text,
                "amount": 100000000,  # 0.1 SOL
                "slippage": 0.5  # 0.5%
            }
            self.storage.save_data()

            await update.message.reply_text(
                "✅ Кошелек успешно импортирован!\n\n"
                "Теперь вы можете настроить параметры покупки:",
                reply_markup=self._get_settings_keyboard()
            )
            return ConversationHandler.END
        except Exception as e:
            await update.message.reply_text(
                f"❌ Ошибка: {str(e)}\nПопробуйте еще раз:",
                reply_markup=self._get_back_keyboard()
            )
            return IMPORT_WALLET

    async def _cmd_remove_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Запрос подтверждения удаления кошелька"""
        user_id = str(update.message.from_user.id)

        if user_id not in self.storage.data["wallets"]:
            await update.message.reply_text(
                "ℹ️ У вас нет импортированного кошелька",
                reply_markup=self._get_main_keyboard()
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "⚠️ Вы уверены, что хотите удалить привязанный кошелек?\n\n"
            "Это действие нельзя отменить!",
            reply_markup=self._get_wallet_remove_keyboard()
        )
        return REMOVE_WALLET

    async def _confirm_remove_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Подтверждение удаления кошелька"""
        user_id = str(update.message.from_user.id)

        if user_id in self.storage.data["wallets"]:
            del self.storage.data["wallets"][user_id]
            self.storage.save_data()

            # Удаляем пользователя из всех каналов
            for channel in list(self.storage.data["channels"].keys()):
                if user_id in self.storage.data["channels"][channel]:
                    self.storage.data["channels"][channel].remove(user_id)

                    # Удаляем канал если подписчиков больше нет
                    if not self.storage.data["channels"][channel]:
                        del self.storage.data["channels"][channel]

            self.storage.save_data()

            await update.message.reply_text(
                "✅ Кошелек и все связанные данные успешно удалены!",
                reply_markup=self._get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "ℹ️ Кошелек уже был удален",
                reply_markup=self._get_main_keyboard()
            )

        return ConversationHandler.END

    async def _handle_remove_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка текстового ввода при удалении кошелька"""
        text = update.message.text.lower().strip()

        if text in ["да", "удалить", "yes", "delete"]:
            return await self._confirm_remove_wallet(update, context)
        else:
            await update.message.reply_text(
                "Удаление кошелька отменено",
                reply_markup=self._get_main_keyboard()
            )
            return ConversationHandler.END



    async def _show_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать текущие настройки"""
        user_id = str(update.message.from_user.id)

        if user_id not in self.storage.data["wallets"]:
            await update.message.reply_text(
                "Сначала импортируйте кошелек!",
                reply_markup=self._get_main_keyboard()
            )
            return

        wallet = self.storage.data["wallets"][user_id]
        await update.message.reply_text(
            f"⚙️ Текущие настройки:\n\n"
            f"💰 Сумма покупки: {wallet['amount']} lamports (~{wallet['amount'] / 1000000000:.4f} SOL)\n"
            f"📉 Проскальзывание: {wallet['slippage']}%\n\n"
            "Выберите параметр для изменения:",
            reply_markup=self._get_settings_keyboard()
        )

    async def _cmd_set_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Установка суммы покупки"""
        user_id = str(update.message.from_user.id)

        if user_id not in self.storage.data["wallets"]:
            await update.message.reply_text(
                "Сначала импортируйте кошелек!",
                reply_markup=self._get_main_keyboard()
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "Введите сумму в lamports (1 SOL = 1,000,000,000 lamports):\n\n"
            "Пример: 100000000 = 0.1 SOL",
            reply_markup=self._get_back_keyboard()
        )
        return SET_AMOUNT

    async def _handle_set_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка установки суммы"""
        user_id = str(update.message.from_user.id)
        text = update.message.text.strip()

        # Проверяем, не нажата ли кнопка "Назад"
        if text in ["🔙 Назад", "🔙 На главную", "🔙 Отмена"]:
            await self._back_to_main(update, context)
            return ConversationHandler.END

        try:
            amount = int(text)
            if amount <= 0:
                raise ValueError("Сумма должна быть положительной")

            self.storage.data["wallets"][user_id]["amount"] = amount
            self.storage.save_data()

            await update.message.reply_text(
                f"✅ Сумма покупки установлена: {amount} lamports (~{amount / 1000000000:.4f} SOL)\n\n"
                "Что дальше?",
                reply_markup=self._get_settings_keyboard()
            )
            return ConversationHandler.END
        except ValueError as e:
            await update.message.reply_text(
                f"❌ Ошибка: {str(e)}\nПопробуйте еще раз:",
                reply_markup=self._get_back_keyboard()
            )
            return SET_AMOUNT

    async def _cmd_set_slippage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Установка slippage"""
        user_id = str(update.message.from_user.id)

        if user_id not in self.storage.data["wallets"]:
            await update.message.reply_text(
                "Сначала импортируйте кошелек!",
                reply_markup=self._get_main_keyboard()
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "Введите процент проскальзывания (0.1-50%):\n\n"
            "Рекомендуемое значение: 0.5-2%",
            reply_markup=self._get_back_keyboard()
        )
        return SET_SLIPPAGE

    async def _handle_set_slippage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка установки slippage"""
        user_id = str(update.message.from_user.id)
        text = update.message.text.strip()

        # Проверяем, не нажата ли кнопка "Назад"
        if text in ["🔙 Назад", "🔙 На главную", "🔙 Отмена"]:
            await self._back_to_main(update, context)
            return ConversationHandler.END

        try:
            slippage = float(text)
            if slippage < 0.1 or slippage > 50:
                raise ValueError("Допустимый диапазон: 0.1-50%")

            self.storage.data["wallets"][user_id]["slippage"] = slippage
            self.storage.save_data()

            await update.message.reply_text(
                f"✅ Проскальзывание установлено: {slippage}%\n\n"
                "Что дальше?",
                reply_markup=self._get_settings_keyboard()
            )
            return ConversationHandler.END
        except ValueError as e:
            await update.message.reply_text(
                f"❌ Ошибка: {str(e)}\nПопробуйте еще раз:",
                reply_markup=self._get_back_keyboard()
            )
            return SET_SLIPPAGE

    async def _cmd_list_channels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Список отслеживаемых каналов с кнопками управления"""
        user_id = str(update.message.from_user.id)
        user_channels = [
            channel for channel, users in self.storage.data["channels"].items()
            if user_id in users
        ]

        if not user_channels:
            await update.message.reply_text(
                "ℹ️ Вы пока не добавили ни одного канала",
                reply_markup=self._get_main_keyboard()
            )
            return

        # Создаем сообщение с кнопками для каждого канала
        message = "📋 Ваши каналы:\n\n" + "\n".join(
            f"{i + 1}. {channel}" for i, channel in enumerate(user_channels)
        )

        await update.message.reply_text(
            message,
            reply_markup=self._get_main_keyboard()
        )

    async def _back_to_main(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Возврат в главное меню"""
        await update.message.reply_text(
            "Главное меню:",
            reply_markup=self._get_main_keyboard()
        )
        return ConversationHandler.END

    async def _process_token(self, event, token):
        """Обработка найденного токена"""
        try:
            channel = f"@{event.chat.username}" if event.chat.username else event.chat.title
            message_link = f"https://t.me/c/{event.chat.id}/{event.id}"

            for user_id in self.storage.data["channels"].get(channel, []):
                if user_id in self.storage.data["wallets"]:
                    wallet = self.storage.data["wallets"][user_id]

                    try:
                        result = await self.trader.buy_token(
                            private_key=wallet["private_key"],
                            token=token,
                            amount=wallet["amount"],
                            slippage=wallet["slippage"]
                        )

                        await self.bot.send_message(
                            chat_id=user_id,
                            text=f"✅ Токен куплен!\n\n"
                                 f"▪️ Токен: {token}\n"
                                 f"▪️ Канал: {channel}\n"
                                 f"▪️ Сумма: {wallet['amount'] / 1000000000:.4f} SOL\n"
                                 f"▪️ Slippage: {wallet['slippage']}%\n"
                                 f"▪️ TX: {result.get('hash', 'N/A')}\n"
                                 f"🔗 {message_link}",
                            reply_markup=self._get_main_keyboard()
                        )
                    except Exception as e:
                        await self.bot.send_message(
                            chat_id=user_id,
                            text=f"❌ Ошибка покупки {token}:\n{str(e)}",
                            reply_markup=self._get_main_keyboard()
                        )
        except Exception as e:
            print(f"Ошибка обработки токена: {e}")

async def main():
    required_vars = [
        "TELEGRAM_TOKEN",
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "SOLANA_RPC",
        "GMGN_API"
    ]

    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        print(f"❌ Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}")
        return

    if not os.getenv("ENCRYPTION_KEY"):
        print("⚠️ Внимание: ENCRYPTION_KEY не найден, используется временный ключ шифрования")
        print("Сгенерируйте постоянный ключ командой:")
        print("python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'")

    try:
        bot = TokenHunterBot()
        await bot.run()
    except Exception as e:
        print(f"🔴 Критическая ошибка: {e}")


if __name__ == "__main__":
    asyncio.run(main())
