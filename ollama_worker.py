import sys
import requests
import json
import random
import time
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import pandas_ta as ta
from sklearn.linear_model import LinearRegression
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_absolute_error
import warnings
warnings.filterwarnings('ignore')

# PyQt5 — нужен только для UI-частей (OllamaWorker). На сервере не ставится.
try:
    from PyQt5.QtCore import QThread, pyqtSignal
    _HAS_QT = True
except ImportError:
    _HAS_QT = False

    class QThread:  # type: ignore[no-redef]
        """Заглушка, чтобы модуль импортировался без PyQt5 на сервере."""

        def __init__(self, *args, **kwargs):
            raise RuntimeError("OllamaWorker недоступен без PyQt5")

    def pyqtSignal(*args, **kwargs):  # type: ignore[no-redef]
        return None

# Импорты для парсинга новостей — тоже опциональны (на сервере используется
# server.news_fetcher, без браузера).
try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False
    sync_playwright = None  # type: ignore[assignment]

try:
    from fake_useragent import UserAgent
except ImportError:
    class UserAgent:  # type: ignore[no-redef]
        @property
        def random(self) -> str:
            return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class NewsParser:
    def __init__(self):
        self.news_file = "cbr_news.json"
    
    def random_delay(self, min_sec=1, max_sec=5):
        time.sleep(random.uniform(min_sec, max_sec))

    def smart_scroll_for_new_content(self, page):
        """Умная прокрутка для загрузки нового контента"""
        # Сначала прокручиваем вверх для загрузки новых сообщений
        for i in range(3):
            page.evaluate("window.scrollTo(0, 0)")
            self.random_delay(2, 4)
            
            # Прокручиваем немного вниз и снова вверх
            page.evaluate("window.scrollTo(0, 300)")
            self.random_delay(1, 2)
            page.evaluate("window.scrollTo(0, 0)")
            self.random_delay(2, 3)
        
        # Ждем загрузки нового контента
        self.random_delay(3, 5)
        
        # Проверяем, есть ли новые сообщения
        try:
            page.wait_for_selector(".tgme_widget_message", timeout=10000)
        except:
            print("Не удалось найти сообщения после прокрутки")

    def load_existing_news(self):
        """Загружает существующие новости из JSON файла"""
        try:
            with open(self.news_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def save_unique_news(self, new_news, existing_news):
        """Сохраняет только уникальные новости, объединяя с существующими"""
        existing_links = set()
        for news in existing_news:
            if "link" in news and news["link"]:
                existing_links.add(news["link"])
        
        unique_news = existing_news.copy()
        new_count = 0
        
        for news in new_news:
            if "link" in news and news["link"] and news["link"] not in existing_links:
                unique_news.append(news)
                new_count += 1
                existing_links.add(news["link"])
        
        with open(self.news_file, "w", encoding="utf-8") as f:
            json.dump(unique_news, f, ensure_ascii=False, indent=2)
        
        return new_count, len(unique_news)

    def setup_stealth_page(self, browser):
        context_options = {
            'user_agent': UserAgent().random,
            'viewport': {'width': random.randint(1024, 1920), 'height': random.randint(768, 1080)},
            'locale': 'ru-RU',
            'timezone_id': 'Europe/Moscow',
            'color_scheme': 'light',
            'java_script_enabled': True,
            'geolocation': {'latitude': random.uniform(55.0, 56.0), 'longitude': random.uniform(37.0, 38.0)},
            'permissions': ['geolocation']
        }
        
        context = browser.new_context(**context_options)
        
        # Добавляем случайные куки
        context.add_cookies([{
            'name': f'random_cookie_{random.randint(1, 100)}',
            'value': ''.join(random.choices('abcdefghijklmnopqrstuvwxyz1234567890', k=10)),
            'domain': '.t.me',
            'path': '/',
            'expires': time.time() + 86400
        }])
        
        page = context.new_page()
        
        # Скрываем автоматизацию
        page.add_init_script("""
            delete navigator.__proto__.webdriver;
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3],
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['ru-RU', 'ru', 'en-US', 'en'],
            });
        """)
        
        return page

    def parse_telegram_channel(self):
        if not _HAS_PLAYWRIGHT:
            raise RuntimeError(
                "Playwright не установлен — используйте server.news_fetcher.TelegramNewsFetcher"
            )
        channel_url = "https://t.me/s/cbrstocks"
        max_news = 50
        
        # Загружаем существующие новости
        existing_news = self.load_existing_news()
        print(f"Загружено {len(existing_news)} существующих новостей")
        print(f"Текущая дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        
        existing_links = set()
        for news in existing_news:
            if "link" in news and news["link"]:
                existing_links.add(news["link"])
        
        new_news_list = []
        
        with sync_playwright() as p:
            browser_args = [
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-web-security',
                '--disable-features=IsolateOrigins,site-per-process',
                '--disable-site-isolation-trials',
            ]
            
            browser = p.chromium.launch(
                headless=True,
                args=browser_args,
                slow_mo=random.randint(100, 500),
            )
            
            try:
                page = self.setup_stealth_page(browser)
                page.set_default_timeout(60000)
                
                print(f"Переходим на {channel_url}")
                
                # Загружаем страницу с обходом кэша
                response = page.goto(
                    channel_url, 
                    timeout=120000, 
                    wait_until="domcontentloaded",
                    **{'referer': 'https://t.me/'}
                )
                
                if not response.ok:
                    print(f"Ошибка загрузки страницы: {response.status}")
                    return []
                
                self.random_delay(3, 5)
                
                # Ждем загрузки сообщений
                try:
                    page.wait_for_selector(".tgme_widget_message", timeout=30000)
                    print("Сообщения загружены")
                except Exception as e:
                    print(f"Не удалось загрузить сообщения: {e}")
                    return []
                
                # Загружаем новый контент
                self.smart_scroll_for_new_content(page)
                
                # Парсим сообщения
                messages = page.query_selector_all(".tgme_widget_message")
                print(f"Найдено {len(messages)} сообщений")
                
                # Сортируем сообщения по дате (новые сначала)
                messages_with_dates = []
                for message in messages:
                    try:
                        date_element = message.query_selector(".tgme_widget_message_date time")
                        if date_element:
                            date_str = date_element.get_attribute("datetime")
                            messages_with_dates.append((message, date_str))
                    except:
                        continue
                
                # Сортируем по дате (новые сначала)
                messages_with_dates.sort(key=lambda x: x[1] if x[1] else "", reverse=True)
                
                parsed_count = 0
                for message, date_str in messages_with_dates:
                    if parsed_count >= max_news:
                        break
                        
                    try:
                        text_element = message.query_selector(".tgme_widget_message_text")
                        if not text_element:
                            continue
                            
                        text = text_element.inner_text().strip()
                        
                        link_element = message.query_selector(".tgme_widget_message_date")
                        link = link_element.get_attribute("href") if link_element else ""
                        
                        # Пропускаем если уже есть в базе
                        if link and link in existing_links:
                            continue
                        
                        # Парсим дату
                        if date_str:
                            try:
                                # Преобразуем ISO строку в читаемый формат
                                date_obj = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                                formatted_date = date_obj.strftime('%Y-%m-%d %H:%M:%S')
                            except:
                                formatted_date = date_str
                        else:
                            formatted_date = "Неизвестно"
                        
                        news_item = {
                            "text": text,
                            "date": formatted_date,
                            "link": link,
                            "parsed_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        }
                        
                        # Проверяем на дубликаты в текущем парсинге
                        if not any(item["link"] == link for item in new_news_list):
                            new_news_list.append(news_item)
                            parsed_count += 1
                            print(f"Новая новость: {formatted_date} - {text[:100]}...")
                            
                        self.random_delay(0.5, 1)
                            
                    except Exception as e:
                        print(f"Ошибка при парсинге сообщения: {e}")
                        continue
                
                # Сохраняем результаты
                if new_news_list:
                    new_count, total_count = self.save_unique_news(new_news_list, existing_news)
                    print(f"\nУспешно сохранено {new_count} новых новостей")
                    print(f"Всего в базе: {total_count} новостей")
                    
                    # Показываем последние 3 новые новости
                    print("\nПоследние новые новости:")
                    for i, news in enumerate(new_news_list[:3]):
                        print(f"{i+1}. {news['date']}: {news['text'][:100]}...")
                else:
                    print("Новых новостей не найдено")
                
                return new_news_list
                
            except Exception as e:
                print(f"Произошла ошибка: {e}")
                return []
            finally:
                browser.close()


class PricePredictor:
    """Класс для прогнозирования цен акций"""
    
    def __init__(self):
        self.models = {}
        self.scalers = {}
        self.prediction_history = {}  # История прогнозов для оценки точности
        self.feature_history = []  # История признаков для анализа
        
    def create_features(self, data):
        """Создает признаки для прогнозирования"""
        df = data.copy()
        
        # Лаговые признаки
        for lag in [1, 2, 3, 5, 10, 15, 20]:
            df[f'close_lag_{lag}'] = df['CLOSE'].shift(lag)
            df[f'volume_lag_{lag}'] = df['VOL'].shift(lag)
            if lag in [1, 5, 10]:
                df[f'return_lag_{lag}'] = df['CLOSE'].pct_change(lag)
        
        # Технические индикаторы
        df['SMA_5'] = ta.sma(df['CLOSE'], length=5)
        df['SMA_10'] = ta.sma(df['CLOSE'], length=10)
        df['SMA_20'] = ta.sma(df['CLOSE'], length=20)
        df['EMA_5'] = ta.ema(df['CLOSE'], length=5)
        df['EMA_10'] = ta.ema(df['CLOSE'], length=10)
        df['RSI_14'] = ta.rsi(df['CLOSE'], length=14)
        
        # Добавляем: относительная сила по сравнению с IMOEX (если доступен)
        if getattr(self, 'market_data', None) is not None:
            try:
                # Предполагаем, что market_data содержит данные IMOEX
                imoex_returns = self.market_data['CLOSE'].pct_change()
                stock_returns = df['CLOSE'].pct_change()
                df['relative_strength'] = stock_returns - imoex_returns
            except:
                df['relative_strength'] = 0
        else:
            df['relative_strength'] = df['CLOSE'].pct_change()
        
        # Добавляем: коэффициент ликвидности
        if 'VOL' in df.columns:
            df['volume_sma_20'] = df['VOL'].rolling(20).mean()
            df['liquidity_ratio'] = df['VOL'] / df['volume_sma_20'].replace(0, 1)
            df['liquidity_ratio'] = df['liquidity_ratio'].fillna(1)
        else:
            df['liquidity_ratio'] = 1
        
        # Волатильность
        df['price_range'] = df['HIGH'] - df['LOW']
        df['price_range_norm'] = df['price_range'] / df['CLOSE']
        df['volatility_5'] = df['CLOSE'].pct_change().rolling(5).std()
        df['volatility_10'] = df['CLOSE'].pct_change().rolling(10).std()
        df['atr_14'] = ta.atr(df['HIGH'], df['LOW'], df['CLOSE'], length=14)
        
        # День недели и месяц (если есть даты)
        if 'DATE' in df.columns:
            df['day_of_week'] = df['DATE'].dt.dayofweek
            df['month'] = df['DATE'].dt.month
            df['is_month_end'] = df['DATE'].dt.is_month_end.astype(int)
            df['is_month_start'] = df['DATE'].dt.is_month_start.astype(int)
        
        # Трендовые признаки
        df['trend_5'] = (df['CLOSE'] - df['CLOSE'].shift(5)) / df['CLOSE'].shift(5)
        df['trend_10'] = (df['CLOSE'] - df['CLOSE'].shift(10)) / df['CLOSE'].shift(10)
        
        # Объемные признаки
        df['volume_ratio'] = df['VOL'] / df['VOL'].rolling(20).mean()
        df['volume_price_trend'] = df['VOL'] * df['CLOSE'].pct_change()
        
        # Целевая переменная - цена на следующий день
        df['target'] = df['CLOSE'].shift(-1)
        
        # Сохраняем историю признаков для анализа
        if not df.empty and 'DATE' in df.columns:
            self.feature_history = df[['DATE', 'CLOSE', 'target']].dropna().tail(100).copy()
        
        return df.dropna()
    
    def train_models(self, data):
        """Обучает модели прогнозирования с взвешенным ансамблированием"""
        if len(data) < 30:
            return None, None, None, None, None, None
        
        # Создаем признаки
        df = self.create_features(data)
        
        if len(df) < 25:
            return None, None, None, None, None, None
            
        # Разделяем на признаки и целевую переменную
        feature_columns = [col for col in df.columns if col not in ['DATE', 'target', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOL']]
        X = df[feature_columns]
        y = df['target']
        
        if len(X) < 20:
            return None, None, None, None, None, None
            
        # Рассчитываем историческую волатильность для ограничения диапазона прогноза
        if len(data) >= 30:
            returns = data['CLOSE'].pct_change().dropna()
            historical_volatility = returns.std() * np.sqrt(252)  # Годовая волатильность
            daily_volatility = returns.std()  # Дневная волатильность
        else:
            historical_volatility = data['CLOSE'].pct_change().std() * np.sqrt(252)
            daily_volatility = data['CLOSE'].pct_change().std()
        
        # Масштабируем признаки
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        
        # Оцениваем точность моделей на последних 20 днях для взвешенного ансамблирования
        if len(df) >= 40:
            # Используем последние 20 дней для валидации
            test_size = min(20, len(df) // 5)
            X_train = X_scaled[:-test_size]
            y_train = y[:-test_size]
            X_test = X_scaled[-test_size:]
            y_test = y[-test_size:]
            
            # Обучаем LinearRegression
            lr_model = LinearRegression()
            lr_model.fit(X_train, y_train)
            lr_pred = lr_model.predict(X_test)
            lr_mae = mean_absolute_error(y_test, lr_pred)
            lr_accuracy = max(0, 100 - (lr_mae / y_test.mean() * 100)) if y_test.mean() > 0 else 0
            
            # Обучаем RandomForestRegressor с оптимизированными параметрами
            rf_model = RandomForestRegressor(
                n_estimators=100,
                max_depth=8,
                min_samples_split=5,
                min_samples_leaf=2,
                random_state=42,
                max_features='sqrt',
                bootstrap=True
            )
            rf_model.fit(X_train, y_train)
            rf_pred = rf_model.predict(X_test)
            rf_mae = mean_absolute_error(y_test, rf_pred)
            rf_accuracy = max(0, 100 - (rf_mae / y_test.mean() * 100)) if y_test.mean() > 0 else 0
            
            # Рассчитываем веса на основе точности
            total_accuracy = lr_accuracy + rf_accuracy
            if total_accuracy > 0:
                lr_weight = lr_accuracy / total_accuracy
                rf_weight = rf_accuracy / total_accuracy
            else:
                lr_weight = 0.5
                rf_weight = 0.5

            # Доля дней на отложенной выборке, когда ансамбль угадал направление
            # (вырастет цена завтра или упадёт). «100 − MAPE» выше для цены
            # акции всегда около 98% и о качестве прогноза ничего не говорит.
            ensemble_test = lr_pred * lr_weight + rf_pred * rf_weight
            close_test = df['CLOSE'].values[-test_size:]
            hits = np.sign(ensemble_test - close_test) == np.sign(y_test.values - close_test)
            direction_accuracy = float(hits.mean() * 100)

            # Переобучаем на всех данных с сохраненными весами
            lr_model_full = LinearRegression()
            lr_model_full.fit(X_scaled, y)
            
            rf_model_full = RandomForestRegressor(
                n_estimators=100,
                max_depth=8,
                min_samples_split=5,
                min_samples_leaf=2,
                random_state=42,
                max_features='sqrt',
                bootstrap=True
            )
            rf_model_full.fit(X_scaled, y)
            
            return (lr_model_full, rf_model_full, scaler, feature_columns,
                   (lr_weight, rf_weight), (lr_accuracy, rf_accuracy),
                   daily_volatility, historical_volatility, direction_accuracy)
        else:
            # Если данных мало, используем равные веса
            lr_model = LinearRegression()
            lr_model.fit(X_scaled, y)
            
            rf_model = RandomForestRegressor(
                n_estimators=50,
                max_depth=5,
                random_state=42
            )
            rf_model.fit(X_scaled, y)
            
            # Отложенной выборки нет — точность направления неизвестна
            return (lr_model, rf_model, scaler, feature_columns,
                   (0.5, 0.5), (75, 75),
                   daily_volatility, historical_volatility, None)
    
    def predict_next_day(self, data, current_price):
        """Прогнозирует цену на следующий день с оценкой точности"""
        try:
            if len(data) < 30:
                return None, None, "Недостаточно данных для прогноза"
            
            # Обучаем модели с улучшенным методом
            result = self.train_models(data)
            if result[0] is None:
                return None, None, "Ошибка обучения моделей"
            
            lr_model, rf_model, scaler, feature_columns, weights, accuracies, daily_vol, hist_vol, model_direction_accuracy = result
            lr_weight, rf_weight = weights
            lr_accuracy, rf_accuracy = accuracies
            
            # Создаем признаки для последней доступной точки
            df_features = self.create_features(data)
            if len(df_features) == 0 or feature_columns[0] not in df_features.columns:
                return None, None, "Ошибка создания признаков"
            
            last_features = df_features.iloc[-1:][feature_columns]
            if len(last_features) == 0:
                return None, None, "Ошибка создания признаков"
            
            # Масштабируем признаки
            last_features_scaled = scaler.transform(last_features)
            
            # Делаем прогнозы
            lr_pred = lr_model.predict(last_features_scaled)[0]
            rf_pred = rf_model.predict(last_features_scaled)[0]
            
            # Взвешенное ансамблирование
            avg_pred = (lr_pred * lr_weight) + (rf_pred * rf_weight)
            
            # Ограничиваем прогноз диапазоном на основе исторической волатильности
            volatility_range = current_price * hist_vol * 2
            min_price = current_price - volatility_range
            max_price = current_price + volatility_range
            avg_pred = np.clip(avg_pred, min_price, max_price)
            
            # Рассчитываем метрики точности направления за последние 30 дней
            direction_accuracy = 0
            mae_30d = 0
            if len(data) >= 60:  # Нужно как минимум 60 дней для оценки 30-дневной истории
                direction_correct = 0
                total_days = 0
                errors = []
                
                # Анализируем последние 30 дней для оценки точности
                for i in range(len(data)-30, len(data)-1):
                    if i < 20:
                        continue
                    
                    train_data = data.iloc[:i+1]
                    test_point = data.iloc[i+1]
                    
                    try:
                        # Быстрое обучение на усеченных данных
                        if len(train_data) >= 30:
                            # Используем упрощенный прогноз для оценки направления
                            actual_change = (test_point['CLOSE'] - train_data['CLOSE'].iloc[-1]) / train_data['CLOSE'].iloc[-1]
                            
                            # Простой прогноз на основе тренда
                            recent_trend = train_data['CLOSE'].tail(5).pct_change().mean()
                            predicted_change = recent_trend if not np.isnan(recent_trend) else 0
                            
                            # Проверяем правильность направления
                            if (actual_change > 0 and predicted_change > 0) or (actual_change < 0 and predicted_change < 0):
                                direction_correct += 1
                            
                            # Рассчитываем ошибку
                            predicted_price = train_data['CLOSE'].iloc[-1] * (1 + predicted_change)
                            errors.append(abs(predicted_price - test_point['CLOSE']))
                            total_days += 1
                    except:
                        continue
                
                if total_days > 0:
                    direction_accuracy = (direction_correct / total_days) * 100
                    mae_30d = np.mean(errors) if errors else 0
            
            # Рассчитываем оценку уверенности
            confidence_score = 0
            if lr_accuracy > 0 and rf_accuracy > 0:
                # Базовый score на основе точности моделей
                base_score = (lr_accuracy * lr_weight + rf_accuracy * rf_weight)
                
                # Корректировка на согласованность моделей
                model_agreement = 1 - (abs(lr_pred - rf_pred) / current_price)
                agreement_factor = max(0, min(1, model_agreement))
                
                # Корректировка на историческую точность направления
                direction_factor = direction_accuracy / 100 if direction_accuracy > 0 else 0.5
                
                # Рассчитываем итоговый confidence score
                confidence_score = base_score * 0.6 + (base_score * agreement_factor) * 0.3 + (base_score * direction_factor) * 0.1
                confidence_score = min(100, max(0, confidence_score))
            
            # Рассчитываем общую точность
            avg_accuracy = (lr_accuracy * lr_weight + rf_accuracy * rf_weight)
            
            # Формируем детализированный отчет
            details = f"""
Прогноз основан на:
1. Взвешенном ансамблировании моделей (LinearRegression: {lr_accuracy:.1f}%, RandomForest: {rf_accuracy:.1f}%)
2. Исторической волатильности: {hist_vol*100:.1f}% годовых
3. Ограничен диапазоном: [{min_price:.2f}, {max_price:.2f}]
4. Точность направления наивного трендового прогноза за 30 дней: {direction_accuracy:.1f}%
5. Средняя абсолютная ошибка за 30 дней: {mae_30d:.2f} руб
6. Уверенность модели: {confidence_score:.1f}%
7. Модель угадывает направление на отложенной выборке: {f"{model_direction_accuracy:.1f}%" if model_direction_accuracy is not None else "н/д"}
"""

            # Наружу отдаём точность направления: по ней агент решает,
            # доверять ли прогнозу (порог 55%), а UI показывает её пользователю.
            return avg_pred, model_direction_accuracy, details
            
        except Exception as e:
            return None, None, f"Ошибка прогнозирования: {str(e)}"


class OllamaWorker(QThread):
    response_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    
    def __init__(self, prompt, stock_context="", news_data=None, stock_data=None, stock_symbol="", 
                 market_sentiment="", all_stocks_data=None, prediction_type="single", currency_rates=None):
        super().__init__()
        self.prompt = prompt
        self.stock_context = stock_context
        self.news_data = news_data or []
        self.stock_data = stock_data
        self.stock_symbol = stock_symbol
        self.market_sentiment = market_sentiment
        self.all_stocks_data = all_stocks_data or {}
        self.prediction_type = prediction_type
        self.currency_rates = currency_rates or {}
        self.base_url = "http://localhost:11435/api/generate"
        
    def run(self):
        try:
            # Загружаем последние новости
            recent_news = self.load_recent_news(50)
            
            # Анализируем валютные риски
            currency_analysis = self.analyze_currency_impact()
            
            # Анализируем политические риски
            political_analysis = self.analyze_political_risks(recent_news)
            
            if self.prediction_type == "single":
                # Анализ для одной акции
                trading_analysis = self.analyze_trading_data()
                market_analysis = self.analyze_market_sentiment()
                ml_prediction = self.get_ml_prediction()
                volatility_analysis = self.analyze_volatility()
                
                full_prompt = self.build_single_stock_prompt(
                    recent_news, trading_analysis, market_analysis, 
                    ml_prediction, currency_analysis, political_analysis,
                    volatility_analysis
                )
            else:
                # Анализ для всех акций
                volatility_analysis = self.analyze_market_volatility()
                full_prompt = self.build_all_stocks_prompt(
                    recent_news, currency_analysis, 
                    political_analysis, volatility_analysis
                )
            
            data = {
                "model": "my-llama-8b:latest",
                "prompt": full_prompt,
                "stream": False,
                "options": {
                    "temperature": 0.2,  # Ещё меньше для большей точности
                    "top_p": 0.7,
                    "top_k": 25
                }
            }
            
            response = requests.post(self.base_url, json=data, timeout=90)
            
            if response.status_code == 200:
                result = response.json()
                self.response_received.emit(result['response'])
            else:
                self.error_occurred.emit(f"Ошибка API: {response.status_code}")
                
        except requests.exceptions.ConnectionError:
            self.error_occurred.emit("Не удается подключиться к Ollama. Убедитесь, что Ollama запущен на localhost:11434")
        except requests.exceptions.Timeout:
            self.error_occurred.emit("Таймаут запроса к Ollama")
        except Exception as e:
            self.error_occurred.emit(f"Ошибка: {str(e)}")

    def load_recent_news(self, count=50):
        """Загружает последние новости из файла"""
        try:
            parser = NewsParser()
            all_news = parser.load_existing_news()
            # Сортируем по дате (новые сначала) и берем последние count
            all_news_sorted = sorted(all_news, key=lambda x: x.get('date', ''), reverse=True)
            return all_news_sorted[:count]
        except Exception as e:
            print(f"Ошибка загрузки новостей: {e}")
            return []
    
    def get_ml_prediction(self):
        """Получает прогноз от ML модели для сравнения"""
        try:
            if self.stock_data is None or len(self.stock_data) < 30:
                return "Недостаточно данных для ML прогноза"
            
            predictor = PricePredictor()
            current_price = self.stock_data['CLOSE'].iloc[-1]
            predicted_price, accuracy, details = predictor.predict_next_day(self.stock_data, current_price)
            
            if predicted_price is not None:
                change_percent = ((predicted_price - current_price) / current_price) * 100
                return f"ML прогноз: {predicted_price:.2f} руб ({change_percent:+.2f}%), точность: {accuracy:.1f}%"
            else:
                return "ML прогноз недоступен"
        except Exception as e:
            return f"Ошибка ML прогноза: {str(e)}"
    
    def analyze_volatility(self):
        """Анализирует волатильность акции"""
        if self.stock_data is None or len(self.stock_data) < 20:
            return "Недостаточно данных для анализа волатильности"
        
        try:
            # Историческая волатильность (стандартное отклонение дневных изменений)
            daily_returns = self.stock_data['CLOSE'].pct_change()
            historical_volatility = daily_returns.std() * np.sqrt(252) * 100
            
            # ATR (Average True Range) для оценки внутридневной волатильности
            high_low = self.stock_data['HIGH'] - self.stock_data['LOW']
            high_close = np.abs(self.stock_data['HIGH'] - self.stock_data['CLOSE'].shift())
            low_close = np.abs(self.stock_data['LOW'] - self.stock_data['CLOSE'].shift())
            tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            atr = tr.rolling(14).mean().iloc[-1]
            atr_percent = (atr / self.stock_data['CLOSE'].iloc[-1]) * 100
            
            # Бета к рынку (если есть данные индекса)
            beta_risk = ""
            if 'IMOEX' in self.all_stocks_data and len(self.stock_data) >= 30:
                # Упрощенный расчет бета
                stock_returns = daily_returns.dropna()
                if len(stock_returns) > 20:
                    # Предполагаем, что у нас есть данные IMOEX
                    beta_risk = " | Бета: средняя"
            
            volatility_level = "высокая" if historical_volatility > 30 else "средняя" if historical_volatility > 20 else "низкая"
            
            return f"Волатильность: {volatility_level} ({historical_volatility:.1f}% годовых, ATR: {atr_percent:.1f}%){beta_risk}"
            
        except Exception as e:
            return f"Ошибка анализа волатильности: {str(e)}"
    
    def analyze_market_volatility(self):
        """Анализирует волатильность всего рынка"""
        if not self.all_stocks_data or len(self.all_stocks_data) < 10:
            return "Недостаточно данных для анализа волатильности рынка"
        
        try:
            # Собираем изменения всех акций
            changes = []
            for symbol, data in self.all_stocks_data.items():
                if 'change' in data:
                    changes.append(abs(data['change']))
            
            if changes:
                avg_abs_change = np.mean(changes)
                volatility_index = "📈 ВЫСОКАЯ" if avg_abs_change > 2.0 else "📊 СРЕДНЯЯ" if avg_abs_change > 1.0 else "📉 НИЗКАЯ"
                return f"Волатильность рынка: {volatility_index} (среднее изменение: {avg_abs_change:.2f}%)"
            
            return "Волатильность рынка: данные недоступны"
        except:
            return "Волатильность рынка: ошибка анализа"
    
    def analyze_trading_data(self):
        """Анализирует торговые данные для текущей акции"""
        if self.stock_data is None or len(self.stock_data) < 10:
            return "Недостаточно данных для анализа"
        
        try:
            analysis = []
            current_data = self.stock_data.iloc[-1]
            
            # Базовые метрики
            current_price = current_data['CLOSE']
            analysis.append(f"Цена: {current_price:.2f} руб")
            
            # Анализ тренда
            if len(self.stock_data) >= 5:
                # Краткосрочный тренд (5 дней)
                short_trend = self.calculate_trend(self.stock_data, 5)
                # Среднесрочный тренд (20 дней)
                medium_trend = self.calculate_trend(self.stock_data, min(20, len(self.stock_data)))
                
                analysis.append(f"Тренд: {short_trend} (5д), {medium_trend} (20д)")
            
            # Анализ объема
            if 'VOL' in current_data:
                volume = current_data['VOL']
                avg_volume = self.stock_data['VOL'].tail(20).mean() if len(self.stock_data) >= 20 else volume
                volume_ratio = volume / avg_volume if avg_volume > 0 else 1
                volume_status = "📈 Высокий" if volume_ratio > 1.5 else "📉 Низкий" if volume_ratio < 0.7 else "⚪ Норма"
                analysis.append(f"Объем: {volume_ratio:.1f}x от среднего ({volume_status})")
            
            # Технические индикаторы с весами
            indicator_score = 0
            max_score = 0
            
            if 'RSI' in current_data and not pd.isna(current_data['RSI']):
                rsi = current_data['RSI']
                if rsi < 30:
                    rsi_status = "🟢 Сильная перепроданность"
                    indicator_score += 2
                elif rsi > 70:
                    rsi_status = "🔴 Сильная перекупленность" 
                    indicator_score -= 2
                elif rsi < 40:
                    rsi_status = "🟢 Перепроданность"
                    indicator_score += 1
                elif rsi > 60:
                    rsi_status = "🔴 Перекупленность"
                    indicator_score -= 1
                else:
                    rsi_status = "⚪ Нейтрально"
                analysis.append(f"RSI: {rsi:.1f} ({rsi_status})")
                max_score += 2
            
            if 'MACD_12_26_9' in current_data and not pd.isna(current_data['MACD_12_26_9']):
                macd = current_data['MACD_12_26_9']
                macd_signal = current_data.get('MACDs_12_26_9', 0)
                if macd > macd_signal and macd > 0:
                    macd_status = "🟢 Сильный бычий"
                    indicator_score += 2
                elif macd < macd_signal and macd < 0:
                    macd_status = "🔴 Сильный медвежий"
                    indicator_score -= 2
                elif macd > macd_signal:
                    macd_status = "🟢 Бычий"
                    indicator_score += 1
                else:
                    macd_status = "🔴 Медвежий"
                    indicator_score -= 1
                analysis.append(f"MACD: {macd:.3f} ({macd_status})")
                max_score += 2
            
            # Уровни поддержки и сопротивления
            support, resistance = self.calculate_support_resistance()
            if support and resistance:
                analysis.append(f"Уровни: поддержка {support:.2f}, сопротивление {resistance:.2f}")
            
            # Общая оценка индикаторов
            if max_score > 0:
                indicator_percent = (indicator_score / max_score) * 100
                if indicator_percent > 60:
                    analysis.append("📊 Индикаторы: 🟢 Позитивные")
                elif indicator_percent < -60:
                    analysis.append("📊 Индикаторы: 🔴 Негативные")
                else:
                    analysis.append("📊 Индикаторы: ⚪ Нейтральные")
            
            return " | ".join(analysis)
            
        except Exception as e:
            return f"Ошибка анализа данных: {str(e)}"
    
    def calculate_trend(self, data, period):
        """Рассчитывает тренд за указанный период"""
        if len(data) < period:
            return "Недостаточно данных"
        
        recent_data = data.tail(period)
        price_change = ((recent_data['CLOSE'].iloc[-1] - recent_data['CLOSE'].iloc[0]) / recent_data['CLOSE'].iloc[0]) * 100
        
        if price_change > 3:
            return f"🟢 Рост {price_change:+.1f}%"
        elif price_change < -3:
            return f"🔴 Падение {price_change:+.1f}%"
        else:
            return f"⚪ Боковик {price_change:+.1f}%"
    
    def calculate_support_resistance(self):
        """Рассчитывает уровни поддержки и сопротивления"""
        if self.stock_data is None or len(self.stock_data) < 20:
            return None, None
        
        try:
            recent_data = self.stock_data.tail(20)
            support = recent_data['LOW'].min()
            resistance = recent_data['HIGH'].max()
            current_price = recent_data['CLOSE'].iloc[-1]
            
            # Корректируем уровни на основе текущей цены
            if abs(current_price - support) < (resistance - support) * 0.1:
                support = recent_data['LOW'].nsmallest(2).iloc[-1]  # Второй минимум
            
            if abs(resistance - current_price) < (resistance - support) * 0.1:
                resistance = recent_data['HIGH'].nlargest(2).iloc[-1]  # Второй максимум
            
            return support, resistance
        except:
            return None, None
    
    def analyze_currency_impact(self):
        """Анализирует влияние валютных курсов на акции"""
        if not self.currency_rates:
            return "Курсы валют не загружены"
        
        try:
            analysis = []
            impact_score = 0
            
            # USD/RUB влияние
            usd_rate = self.currency_rates.get('USD', 0)
            if usd_rate > 90:
                analysis.append("💰 USD/RUB > 90: негативно для импортёров")
                impact_score -= 1
            elif usd_rate < 80:
                analysis.append("💰 USD/RUB < 80: позитивно для импортёров")
                impact_score += 1
            
            # EUR/RUB влияние
            eur_rate = self.currency_rates.get('EUR', 0)
            if eur_rate > 100:
                analysis.append("💶 EUR/RUB > 100: давление на еврооблигации")
                impact_score -= 1
            
            # CNY/RUB влияние
            cny_rate = self.currency_rates.get('CNY', 0)
            if cny_rate > 12:
                analysis.append("💴 CNY/RUB > 12: влияние на азиатскую торговлю")
                impact_score -= 0.5
            
            # Общая оценка
            if impact_score > 0.5:
                currency_sentiment = "🟢 Позитивное"
            elif impact_score < -0.5:
                currency_sentiment = "🔴 Негативное"
            else:
                currency_sentiment = "⚪ Нейтральное"
            
            if analysis:
                return f"{currency_sentiment} влияние валют: " + " | ".join(analysis)
            else:
                return "Влияние валют: нейтральное"
                
        except Exception as e:
            return f"Ошибка анализа валют: {str(e)}"
    
    def analyze_political_risks(self, news_data):
        """Анализирует политические риски из новостей"""
        if not news_data:
            return "Политические риски: данные отсутствуют"
        
        try:
            political_keywords = {
                'высокий': ['путин', 'санкции', 'эмбарго', 'запрет', 'ограничения', 
                           'встреча', 'переговоры', 'дипломатия', 'политика', 'кремль',
                           'белый дом', 'нато', 'евросоюз', 'минфин', 'цб'],
                'средний': ['закон', 'регулирование', 'налог', 'пошлина', 'субсидия',
                           'правительство', 'министерство', 'парламент', 'выборы'],
                'низкий': ['заявление', 'комментарий', 'интервью', 'пресс-конференция']
            }
            
            high_risk_news = []
            medium_risk_news = []
            
            for news in news_data[:20]:  # Проверяем последние 20 новостей
                text = news.get('text', '').lower()
                date = news.get('date', '')
                
                # Проверяем высокорисковые ключевые слова
                if any(keyword in text for keyword in political_keywords['высокий']):
                    # Особо опасные комбинации
                    dangerous_combinations = [
                        ('санкции', 'новые'),
                        ('эмбарго', 'нефть'),
                        ('путин', 'заявление'),
                        ('сша', 'россия')
                    ]
                    
                    is_dangerous = any(all(word in text for word in combo) for combo in dangerous_combinations)
                    
                    if is_dangerous:
                        high_risk_news.append(f"🔴 {date[:10]}: {text[:80]}...")
                    else:
                        medium_risk_news.append(f"🟡 {date[:10]}: {text[:60]}...")
            
            if high_risk_news:
                return "🔴 ВЫСОКИЕ политические риски:\n" + "\n".join(high_risk_news[:3])
            elif medium_risk_news:
                return "🟡 Средние политические риски:\n" + "\n".join(medium_risk_news[:3])
            else:
                return "🟢 Низкие политические риски"
                
        except Exception as e:
            return f"Ошибка анализа политических рисков: {str(e)}"
    
    def analyze_market_sentiment(self):
        """Анализирует общее настроение рынка с учетом секторов"""
        if not self.all_stocks_data:
            return "Данные по рынку недоступны"
        
        try:
            positive_stocks = 0
            strong_positive = 0
            strong_negative = 0
            total_stocks = len(self.all_stocks_data)
            total_change = 0
            
            # Анализ по секторам
            sector_performance = {
                "Финансы": {"positive": 0, "total": 0, "avg_change": 0},
                "НефтьГаз": {"positive": 0, "total": 0, "avg_change": 0},
                "Энергетика": {"positive": 0, "total": 0, "avg_change": 0},
                "Металлургия": {"positive": 0, "total": 0, "avg_change": 0},
                "Потребительский": {"positive": 0, "total": 0, "avg_change": 0},
                "Технологии": {"positive": 0, "total": 0, "avg_change": 0},
                "Другие": {"positive": 0, "total": 0, "avg_change": 0}
            }
            
            sector_map = {
                "SBER": "Финансы", "VTBR": "Финансы", "TCSG": "Финансы", "MOEX": "Финансы", "CBOM": "Финансы", "QIWI": "Финансы",
                "GAZP": "НефтьГаз", "LKOH": "НефтьГаз", "ROSN": "НефтьГаз", "TATN": "НефтьГаз", "NVTK": "НефтьГаз", "SNGS": "НефтьГаз", "SNGSP": "НефтьГаз",
                "IRAO": "Энергетика", "HYDR": "Энергетика", "FEES": "Энергетика", "UPRO": "Энергетика",
                "GMKN": "Металлургия", "CHMF": "Металлургия", "RUAL": "Металлургия", "ALRS": "Металлургия", "PLZL": "Металлургия",
                "MGNT": "Потребительский", "X5": "Потребительский", "FIXP": "Потребительский", "AFKS": "Потребительский",
                "YNDX": "Технологии", "OZON": "Технологии", "VKCO": "Технологии", "MTSS": "Технологии", "RTKM": "Технологии",
            }
            
            for symbol, data in self.all_stocks_data.items():
                change = data.get('change', 0)
                if change > 0:
                    positive_stocks += 1
                if change > 2:  # Сильный рост
                    strong_positive += 1
                elif change < -2:  # Сильное падение
                    strong_negative += 1
                total_change += change
                
                # Анализ по секторам
                sector = sector_map.get(symbol, "Другие")
                sector_performance[sector]["total"] += 1
                if change > 0:
                    sector_performance[sector]["positive"] += 1
                sector_performance[sector]["avg_change"] += change
            
            # Рассчитываем средние изменения по секторам
            for sector in sector_performance:
                if sector_performance[sector]["total"] > 0:
                    sector_performance[sector]["avg_change"] /= sector_performance[sector]["total"]
            
            if total_stocks > 0:
                positive_ratio = (positive_stocks / total_stocks) * 100
                avg_change = total_change / total_stocks
                
                # Находим лучший и худший сектора
                best_sector = max([(s, p["avg_change"]) for s, p in sector_performance.items() if p["total"] > 0], 
                                key=lambda x: x[1], default=("Нет данных", 0))
                worst_sector = min([(s, p["avg_change"]) for s, p in sector_performance.items() if p["total"] > 0], 
                                key=lambda x: x[1], default=("Нет данных", 0))
                
                # Более детальная оценка настроения
                if positive_ratio > 70 and avg_change > 1.0 and strong_positive > strong_negative * 2:
                    sentiment = f"📈 СИЛЬНО БЫЧИЙ рынок ({positive_ratio:.1f}% в плюсе)"
                elif positive_ratio > 60 and avg_change > 0.5:
                    sentiment = f"📈 Бычий рынок ({positive_ratio:.1f}% в плюсе)"
                elif positive_ratio < 30 and avg_change < -1.0 and strong_negative > strong_positive * 2:
                    sentiment = f"📉 СИЛЬНО МЕДВЕЖИЙ рынок ({positive_ratio:.1f}% в плюсе)"
                elif positive_ratio < 40 and avg_change < -0.5:
                    sentiment = f"📉 Медвежий рынок ({positive_ratio:.1f}% в плюсе)"
                else:
                    sentiment = f"➡️ НЕЙТРАЛЬНЫЙ рынок ({positive_ratio:.1f}% в плюсе)"
                
                return f"{sentiment} | Лучший сектор: {best_sector[0]} ({best_sector[1]:+.2f}%) | Худший: {worst_sector[0]} ({worst_sector[1]:+.2f}%)"
            
            return "Недостаточно данных для анализа рынка"
            
        except Exception as e:
            return f"Ошибка анализа рынка: {str(e)}"
    
    def calculate_news_impact_score(self, news_list):
        """Рассчитывает совокупное воздействие новостей на акцию"""
        if not news_list:
            return 0, "Новостной фон: нейтральный"
        
        try:
            impact_score = 0
            news_count = len(news_list)
            
            positive_keywords = ['рост', 'прибыль', 'увеличение', 'дивиденды', 'рекомендация', 'покупка', 'цель']
            negative_keywords = ['падение', 'убыток', 'снижение', 'продажа', 'сокращение', 'штраф', 'расследование']
            high_impact_keywords = ['санкции', 'эмбарго', 'запрет', 'путин', 'сша', 'евросоюз', 'кризис']
            
            for news in news_list:
                text = news.get('text', '').lower()
                
                # Высокое воздействие
                if any(keyword in text for keyword in high_impact_keywords):
                    impact_score -= 3  # Высокое негативное воздействие
                
                # Положительное воздействие
                positive_count = sum(1 for keyword in positive_keywords if keyword in text)
                impact_score += positive_count * 0.5
                
                # Негативное воздействие  
                negative_count = sum(1 for keyword in negative_keywords if keyword in text)
                impact_score -= negative_count * 1.0
            
            # Нормализуем и оцениваем
            normalized_score = impact_score / max(news_count, 1)
            
            if normalized_score > 0.5:
                return normalized_score, "📈 Сильный позитивный новостной фон"
            elif normalized_score > 0.2:
                return normalized_score, "📈 Умеренно позитивный новостной фон"
            elif normalized_score < -1.0:
                return normalized_score, "📉 КРИТИЧЕСКИЙ негативный фон"
            elif normalized_score < -0.5:
                return normalized_score, "📉 Сильный негативный фон"
            elif normalized_score < -0.2:
                return normalized_score, "📉 Умеренно негативный фон"
            else:
                return normalized_score, "📊 Нейтральный новостной фон"
                
        except Exception as e:
            return 0, f"Ошибка оценки новостей: {str(e)}"
    
    def build_single_stock_prompt(self, recent_news, trading_analysis, market_analysis, 
                         ml_prediction, currency_analysis, political_analysis,
                         volatility_analysis):
        """Строит улучшенный промпт для анализа одной акции"""
        
        # Форматируем релевантные новости
        news_context = self.format_relevant_news(recent_news, self.stock_symbol)
        
        # Рассчитываем воздействие новостей
        relevant_news_list = []
        symbol_keywords = self._get_stock_keywords(self.stock_symbol)
        
        for news in recent_news[:15]:
            text = news.get('text', '').lower()
            if any(keyword in text for keyword in symbol_keywords):
                relevant_news_list.append(news)
        
        news_impact_score, news_impact_text = self.calculate_news_impact_score(relevant_news_list)

        # Улучшенный промпт с акцентом на точные торговые рекомендации
        full_prompt = f"""
Ты - профессиональный трейдер с 20-летним опытом на российском рынке. Твоя задача - дать КОНКРЕТНЫЕ торговые рекомендации для {self.stock_symbol} на СЛЕДУЮЩИЙ ТОРГОВЫЙ ДЕНЬ.

ЖЁСТКИЕ ОГРАНИЧЕНИЯ:
1. ДНЕВНОЕ ИЗМЕНЕНИЕ редко превышает 3% без форс-мажора
2. Учитывай что 1-2% - нормальная дневная волатильность
3. Политические новости имеют МАКСИМАЛЬНЫЙ вес
4. Курсы валют критически важны для российского рынка

АНАЛИЗ ДАННЫХ для {self.stock_symbol}:
{trading_analysis}

АНАЛИЗ РЫНКА:
{market_analysis}

АНАЛИЗ ВОЛАТИЛЬНОСТИ:
{volatility_analysis}

АНАЛИЗ ВАЛЮТ:
{currency_analysis}

ПОЛИТИЧЕСКИЕ РИСКИ:
{political_analysis}

НОВОСТНОЙ ФОН:
{news_impact_text}
{news_context}

ML ПРОГНОЗ (для сравнения):
{ml_prediction}

ЗАДАЧА: {self.prompt}

КРИТИЧЕСКИ ВАЖНО ДЛЯ ТОРГОВЛИ:
1. ЛОНГ (покупка) только если ожидается рост >1% с вероятностью >60%
2. ШОРТ (продажа) только если ожидается падение >1% с вероятностью >60%
3. Учитывай спред и комиссии - рекомендации должны быть прибыльными
4. Риск-менеджмент: стоп-лосс должен быть на 1-2% ниже ключевых уровней

РАСЧЁТ ТОЧНОЙ ЦЕНЫ:
- Рассчитай ОДНУ наиболее вероятную цену на конец следующего торгового дня
- Учти ближайшие уровни поддержки/сопротивления
- Учти волатильность акции
- Учти объемы торгов
- НЕ давай диапазон цен, только ОДНУ конкретную цену

ФОРМАТ ОТВЕТА (строго придерживайся):
**Прогнозная цена на конец дня:** [одна точная цена] руб ([изменение]% от текущей)
**Рекомендация:** 
🔵 **ЛОНГ:** ПОКУПАТЬ (если ожидается рост)
🔴 **ШОРТ:** ПРОДАВАТЬ (если ожидается падение)
⚪ **НЕЙТРАЛЬНО:** ВНЕ РЫНКА
**Обоснование цены:** [2-3 фактора почему именно эта цена]
**Уровень уверенности:** [высокий/средний/низкий] ([процент]%)
**Параметры сделки:**
- **Точка входа:** [цена_входа] руб
- **Стоп-лосс:** [цена_стоп] руб (-[процент_стоп]%)
- **Тейк-профит:** [цена_тейк] руб (+[процент_тейк]%)
- **Риск-доходность:** 1:[соотношение]
**Ключевые риски:** [конкретные риски с вероятностью]
**Рыночный контекст:** [как эта сделка вписывается в общий рынок]

ПРИМЕР КОНКРЕТНОЙ РЕКОМЕНДАЦИИ:
**Прогнозная цена на конец дня:** 2885 руб (+1.5% от текущей)
**Рекомендация:** 🔵 **ЛОНГ:** ПОКУПАТЬ
**Обоснование цены:** 1) Рост цен на нефть, 2) Слабый рубль, 3) Прорыв уровня сопротивления
**Уровень уверенности:** средний (65%)
**Параметры сделки:**
- **Точка входа:** 2870 руб
- **Стоп-лосс:** 2780 руб (-3.1%)
- **Тейк-профит:** 2950 руб (+2.8%)
- **Риск-доходность:** 1:0.9
**Ключевые риски:** Новые санкции (вероятность 20%, влияние -5%)
**Рыночный контекст:** Акция торгуется в бычьем тренде, рынок нейтральный

Будь КОНКРЕТЕН в цифрах! Давай ОДНУ точную цену для прогноза на конец дня!
"""

        return full_prompt
    
    def _get_base_volatility(self):
        """Возвращает базовую волатильность для акции"""
        # Исторические данные по волатильности акций
        volatility_map = {
            "GAZP": 1.8, "SBER": 2.1, "LKOH": 2.3, "ROSN": 2.4, "YNDX": 3.5,
            "VTBR": 2.8, "TATN": 2.2, "NVTK": 2.5, "GMKN": 3.2, "PLZL": 2.9,
            "MGNT": 1.9, "CHMF": 2.6, "MOEX": 1.7, "RUAL": 3.1, "OZON": 4.2,
            "TCSG": 3.8, "MTSS": 1.8, "IRAO": 1.6, "HYDR": 1.9, "AFLT": 3.5
        }
        return volatility_map.get(self.stock_symbol, 2.0)
    
    def _get_stock_keywords(self, symbol):
        """Возвращает ключевые слова для акции"""
        keywords_map = {
            "MOEX": ["мосбирж", "биржа", "индекс", "imoex", "рынок", "торги"],
            "GAZP": ["газпром", "газ", "нефтегаз", "труба", "миллер", "дивиденды"],
            "SBER": ["сбербанк", "сбер", "банк", "гребенщиков", "ипотека", "кредит"],
            "LKOH": ["лукойл", "нефть", "нефтянка", "алекперов"],
            "ROSN": ["роснефть", "нефть", "сечин"],
            "YNDX": ["яндекс", "поиск", "волож", "аркадий"],
            "VTBR": ["втб", "банк", "костин"],
            "TATN": ["татнефть", "татарстан", "маганов"],
            "NVTK": ["новатэк", "газ", "спг", "михельсон"],
            "GMKN": ["норникель", "никель", "потанин"],
            "PLZL": ["полюс", "золото"],
            "MGNT": ["магнит", "ритейл", "галуца"],
            "CHMF": ["северсталь", "сталь", "мордашов"],
            "SNGS": ["сургутнефтегаз", "сургут", "богданов"],
            "SNGSP": ["сургутнефтегаз-п", "привилегированные"],
            "ALRS": ["алроса", "алмазы", "бриллианты"],
            "RUAL": ["русал", "алюминий", "дерипаска"],
            "AFKS": ["система", "акф", "евткович"],
            "PHOR": ["фосагро", "удобрения", "гурьев"],
            "TCSG": ["ткс", "tinkoff", "тиньков", "банк"],
            "MTSS": ["мтс", "связь", "телеком"],
            "OZON": ["озон", "маркетплейс", "фаль"],
            "QIWI": ["киви", "платежи", "совин"],
            "IRAO": ["интер рао", "энергетика", "севунов"],
            "RTKM": ["ростелеком", "связь", "телеком"],
            "HYDR": ["русгидро", "гидро", "шикунов"],
            "FEES": ["фск", "сети", "ливинский"],
            "AFLT": ["аэрофлот", "авиация", "савельев"],
            "X5": ["икс 5", "питер", "ритейл"],
            "VKCO": ["вк", "vk", "mail.ru", "дуров"],
            "FIXP": ["фикс прайс", "fix price", "дискаунтер"],
            "CBOM": ["мкб", "московский кредитный банк"],
            "LSRG": ["лср", "строительство"],
            "UPRO": ["юнипро", "энергетика"],
            "MSTT": ["мостотрест", "дороги"],
            "KZOS": ["казаньоргсинтез", "химия"]
        }
        return keywords_map.get(symbol, [symbol.lower()])
    
    def get_next_trading_day(self):
        """Возвращает дату следующего торгового дня с учетом выходных"""
        today = datetime.now()
        
        # Список праздничных дней (пример, можно расширить)
        holidays = [
            # Добавьте основные российские праздники
        ]
        
        # Увеличиваем дату на 1 день
        next_day = today + timedelta(days=1)
        
        # Проверяем выходные
        while next_day.weekday() >= 5:  # 5 = суббота, 6 = воскресенье
            next_day += timedelta(days=1)
        
        # Форматируем дату
        return next_day.strftime("%d.%m.%Y")
    
    def build_all_stocks_prompt(self, recent_news, currency_analysis, political_analysis, volatility_analysis):
        """Строит улучшенный промпт для анализа всех акций с торговыми рекомендациями"""
        
        # Группируем акции по секторам для лучшего анализа
        sectors = {
            "💰 Финансы": ["SBER", "VTBR", "TCSG", "MOEX", "CBOM", "QIWI"],
            "🛢️ Нефть и Газ": ["GAZP", "LKOH", "ROSN", "TATN", "NVTK", "SNGS", "SNGSP"],
            "⚡ Энергетика": ["IRAO", "HYDR", "FEES", "UPRO"],
            "🏭 Металлургия": ["GMKN", "CHMF", "RUAL", "ALRS", "PLZL"],
            "🏗️ Промышленность": ["KZOS", "PHOR", "LSRG", "MSTT"],
            "🛒 Потребительский сектор": ["MGNT", "X5", "FIXP", "AFKS"],
            "📱 Технологии": ["YNDX", "OZON", "VKCO", "MTSS", "RTKM"],
            "✈️ Транспорт": ["AFLT"]
        }
        
        stocks_summary = "ДЕТАЛЬНЫЙ АНАЛИЗ КОТИРОВОК ПО СЕКТОРАМ:\n\n"
        
        for sector_name, sector_stocks in sectors.items():
            stocks_summary += f"{sector_name}:\n"
            sector_data = []
            
            for symbol in sector_stocks:
                if symbol in self.all_stocks_data:
                    data = self.all_stocks_data[symbol]
                    change = data.get('change', 0)
                    price = data.get('price', 0)
                    sector_data.append((symbol, change, price))
            
            # Сортируем по изменению (лучшие первыми)
            sector_data.sort(key=lambda x: x[1], reverse=True)
            
            for symbol, change, price in sector_data[:4]:
                change_emoji = "🟢" if change > 0 else "🔴"
                stocks_summary += f"  {change_emoji} {symbol}: {price:.2f} руб ({change:+.2f}%)\n"
            
            stocks_summary += "\n"
        
        # Форматируем новости
        news_context = self.format_general_news(recent_news)
        
        # Получаем дату следующего дня
        next_trading_day = self.get_next_trading_day()

        full_prompt = f"""
Ты - главный портфельный менеджер хедж-фонда. Проанализируй ВСЕ котировки и дай КОНКРЕТНЫЕ торговые рекомендации на СЛЕДУЮЩИЙ ТОРГОВЫЙ ДЕНЬ ({next_trading_day}).

Требуются рекомендации по ДВУМ направлениям:
🔵 **ЛОНГ** - покупка акций с ожиданием роста
🔴 **ШОРТ** - продажа акций с ожиданием падения

ВАЖНОЕ ПРАВИЛО: Для каждой акции давай ОДНУ конкретную прогнозную цену на конец дня, а не диапазон!

{stocks_summary}

{volatility_analysis}

{currency_analysis}

{political_analysis}

{news_context}

ЗАДАЧА: {self.prompt}

ЖЁСТКИЕ ТРЕБОВАНИЯ К РЕКОМЕНДАЦИЯМ:
1. Для ЛОНГА: ожидаемый рост >1.5% с вероятностью >60%
2. Для ШОРТА: ожидаемое падение >1.5% с вероятностью >60%
3. Учитывай комиссии и спред - сделка должна быть прибыльной
4. Риск-менеджмент: стоп-лосс обязателен для каждой рекомендации
5. Максимальный убыток по одной сделке не должен превышать 3%

КРИТЕРИИ ОТБОРА АКЦИЙ:
- Для ЛОНГА: сильный технический сигнал + позитивный фундамент
- Для ШОРТА: слабость на графике + негативные новости
- Избегай акций с низкой ликвидностью
- Приоритет - акции с четкими уровнями стоп-лосса

ФОРМАТ ОТВЕТА (строго придерживайся):
**ТОРГОВЫЕ РЕКОМЕНДАЦИИ НА {next_trading_day}:**

**🔵 ТОП-3 ДЛЯ ЛОНГА (ПОКУПКА):**
1. **[СИМВОЛ]** - Текущая: [цена] руб → Прогноз: [цена] руб (+[процент]%)
- **Вход:** [цена_входа] руб | **Стоп-лосс:** [цена_стоп] руб (-[процент_стоп]%) | **Тейк-профит:** [цена_тейк] руб (+[процент_тейк]%)
- **Обоснование:** [2-3 фактора почему именно эта цена]
- **Уверенность:** [высокая/средняя/низкая] ([процент]%)
- **Риск-доходность:** 1:[соотношение]

2. **[СИМВОЛ]** - Текущая: [цена] руб → Прогноз: [цена] руб (+[процент]%)
- **Вход:** [цена_входа] руб | **Стоп-лосс:** [цена_стоп] руб (-[процент_стоп]%) | **Тейк-профит:** [цена_тейк] руб (+[процент_тейк]%)
- **Обоснование:** [2-3 фактора почему именно эта цена]
- **Уверенность:** [высокая/средняя/низкая] ([процент]%)
- **Риск-доходность:** 1:[соотношение]

**🔴 ТОП-3 ДЛЯ ШОРТА (ПРОДАЖА):**
1. **[СИМВОЛ]** - Текущая: [цена] руб → Прогноз: [цена] руб (-[процент]%)
- **Вход:** [цена_входа] руб | **Стоп-лосс:** [цена_стоп] руб (+[процент_стоп]%) | **Тейк-профит:** [цена_тейк] руб (-[процент_тейк]%)
- **Обоснование:** [2-3 фактора почему именно эта цена]
- **Уверенность:** [высокая/средняя/низкая] ([процент]%)
- **Риск-доходность:** 1:[соотношение]

2. **[СИМВОЛ]** - Текущая: [цена] руб → Прогноз: [цена] руб (-[процент]%)
- **Вход:** [цена_входа] руб | **Стоп-лосс:** [цена_стоп] руб (+[процент_стоп]%) | **Тейк-профит:** [цена_тейк] руб (-[процент_тейк]%)
- **Обоснование:** [2-3 фактора почему именно эта цена]
- **Уверенность:** [высокая/средняя/низкая] ([процент]%)
- **Риск-доходность:** 1:[соотношение]

**📊 ОБЩИЙ РЫНОЧНЫЙ ПРОГНОЗ:**
- **Индекс МосБиржи:** прогноз [одна цена] пунктов ([изменение]%)
- **Лучший сектор для торговли:** [сектор] (причина: [причина])
- **Худший сектор:** [сектор] (причина: [причина])
- **Общая волатильность:** [высокая/средняя/низкая]

**⚡ ЭКСПРЕСС-РЕКОМЕНДАЦИИ:**
- **Самый надежный лонг:** [СИМВОЛ] (низкий риск)
- **Самый прибыльный шорт:** [СИМВОЛ] (высокий потенциал)
- **Акция для скальпинга:** [СИМВОЛ] (высокая волатильность)
- **Акция для долгосрочных позиций:** [СИМВОЛ]

**⚠️ ГЛАВНЫЕ РИСКИ:**
1. [Риск 1] - вероятность [процент]%, влияние [процент]%
2. [Риск 2] - вероятность [процент]%, влияние [процент]%

Будь КОНКРЕТЕН! Давай ОДНУ точную цену для каждой акции, а не диапазон!
"""

        return full_prompt
    
    def format_relevant_news(self, recent_news, symbol):
        """Форматирует релевантные новости с оценкой тональности для торговли"""
        relevant_news = []
        symbol_keywords = self._get_stock_keywords(symbol)
        
        for news in recent_news[:15]:
            text = news.get('text', '').lower()
            if any(keyword in text for keyword in symbol_keywords):
                # Оценка тональности для торговли
                positive_words = ['рост', 'выше', 'позитив', 'улучш', 'прибыль', 'увелич', 'покупк', 
                                'рекоменд', 'перевыполнен', 'успех', 'дивиденд', 'выкуп', 'слияние']
                negative_words = ['падение', 'ниже', 'негатив', 'сниж', 'убыток', 'продаж', 'сокращ', 
                                'слаб', 'проблем', 'риск', 'штраф', 'расследование', 'снижение']
                
                # Критические слова для торговли
                trade_positive = ['покупка', 'лонг', 'перепродан', 'поддержка', 'прорыв']
                trade_negative = ['продажа', 'шорт', 'перекуплен', 'сопротивление', 'пробит']
                
                tone = "⚪"
                trade_signal = ""
                
                if any(word in text for word in positive_words + trade_positive):
                    tone = "🟢"
                    if any(word in text for word in trade_positive):
                        trade_signal = "📈 СИГНАЛ ПОКУПКИ"
                elif any(word in text for word in negative_words + trade_negative):
                    tone = "🔴"
                    if any(word in text for word in trade_negative):
                        trade_signal = "📉 СИГНАЛ ПРОДАЖИ"
                
                relevant_news.append({
                    **news, 
                    'tone': tone,
                    'trade_signal': trade_signal
                })
            
            if len(relevant_news) >= 5:
                break
        
        if relevant_news:
            news_text = "📰 РЕЛЕВАНТНЫЕ НОВОСТИ ДЛЯ ТОРГОВЛИ:\n"
            for i, news in enumerate(relevant_news, 1):
                signal_text = f" {news['trade_signal']}" if news['trade_signal'] else ""
                news_text += f"{news['tone']} {i}. {news.get('text', '')[:100]}...{signal_text}\n"
            return news_text
        else:
            return "📰 Релевантных новостей для торговли не найдено.\n"
    
    def format_general_news(self, recent_news):
        """Форматирует общие рыночные новости с оценкой важности"""
        general_news = []
        important_keywords = ["цб", "ключев", "ставк", "инфляц", "санкц", "эмбарго", "кризис", "рецесс"]
        market_keywords = ["рынок", "индекс", "мосбирж", "акци", "инвест", "экономик"]
        
        for news in recent_news[:10]:
            text = news.get('text', '').lower()
            
            # Оценка важности
            importance = "🔸"
            if any(word in text for word in important_keywords):
                importance = "🔴"
            elif any(word in text for word in market_keywords):
                importance = "🟡"
            
            general_news.append({**news, 'importance': importance})
        
        if general_news:
            news_text = "📊 ОСНОВНЫЕ РЫНОЧНЫЕ НОВОСТИ:\n"
            for i, news in enumerate(general_news, 1):
                news_text += f"{news['importance']} {i}. {news.get('text', '')[:80]}...\n"
            return news_text
        else:
            return "📊 Рыночных новостей не найдено.\n"


if __name__ == "__main__":
    # Тестирование класса
    worker = OllamaWorker("Тестовый запрос")
    print("OllamaWorker успешно создан")