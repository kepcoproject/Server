/*
 * 스마트 에너지 절약 시스템 — ESP32 센서 노드 (MQTT 버전)
 *
 * 기존 HTTP 버전(smart-energy-system/firmware/esp32_sensor_node)을 MQTT로 옮긴 것이다.
 * 센서 읽기 로직(PIR / LDR / CT클램프)은 그대로고, 서버와 주고받는 방식만 바뀌었다.
 *
 * HTTP 버전과 달라진 점
 *   - POST 대신 토픽 발행:  v1/{건물}/{층}/{공간}/data
 *   - LWT 등록:            노드가 죽으면 브로커가 대신 offline 을 알린다
 *   - 제어를 폴링하지 않고 구독: 명령이 즉시 도착한다 (30초 대기 없음)
 *   - 실행 결과를 ack 로 보고: 서버가 '접수'와 '실행'을 구분할 수 있다
 *   - NTP 시각 동기화:      MQTT payload 에 timestamp 가 필수라 반드시 필요하다
 *
 * 필요한 라이브러리 (아두이노 IDE — 라이브러리 매니저에서 설치)
 *   - PubSubClient  (by Nick O'Leary)
 *   - ArduinoJson   (by Benoit Blanchon)
 *
 * ===========================================================================
 * 굽기 전에 아래 "사용자 설정값"을 반드시 채울 것. 그 외는 건드릴 필요 없다.
 * ===========================================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <time.h>

// ===================== 사용자 설정값 (여기만 채우면 된다) =====================
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// MQTT 브로커(mosquitto)가 돌고 있는 PC의 LAN IP.
// localhost 나 127.0.0.1 은 안 된다 — ESP32 입장에서는 자기 자신을 가리킨다.
const char* MQTT_HOST = "192.168.0.10";
const int   MQTT_PORT = 1883;

// 이 노드가 담당하는 공간. 토픽이 이 셋으로 만들어지므로 노드마다 다르게 설정한다.
// 서버의 공간 관리 화면에 그대로 나타난다.
const char* BUILDING = "bldg-a";
const char* FLOOR    = "f2";
const char* ROOM_ID  = "room-101";

// 노드 고유 ID. 같은 공간에 여러 노드를 둘 때 구분용.
const char* DEVICE_ID = "ESP32-NODE-101";

// 측정 주기(ms). 서버는 3분간 소식이 없으면 오프라인으로 본다.
const unsigned long SAMPLE_INTERVAL_MS = 60UL * 1000UL;

// ===================== 핀 설정 =====================
const int PIN_PIR   = 27;  // PIR 디지털 출력
const int PIN_LDR   = 34;  // 조도 센서 아날로그 입력 (ADC1)
const int PIN_CT    = 35;  // CT클램프 아날로그 입력 (ADC1, 버든 저항 통과 후)
const int PIN_RELAY = 26;  // 릴레이 제어 출력

// ===================== 전기 상수 =====================
// SCT-013-030 (30A/1V) 기준 예시값. 실제 버든 저항·분배회로에 맞게 재측정할 것.
const float ADC_VREF = 3.3f;
const int   ADC_RESOLUTION = 4095;    // ESP32 ADC 12bit
const float CT_RATIO = 30.0f / 1.0f;  // 30A : 1V
const int   CT_SAMPLES = 300;         // RMS 계산용 샘플 수

// 전류(A)를 전력(W)으로 바꿀 때 쓰는 선간 전압. 국내 단상 220V.
// 서버도 같은 값을 쓰지만, MQTT payload 는 W 단위라 노드에서 환산해 보낸다.
const float LINE_VOLTAGE = 220.0f;

// ===================== 내부 상태 =====================
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

char topicData[128];
char topicStatus[128];
char topicCmd[128];
char topicAck[128];

unsigned long lastSampleAt = 0;
bool relayOn = true;  // 기본 ON. 서버 명령으로 동기화된다.

// ---------------------------------------------------------------------------
// 토픽 만들기
// ---------------------------------------------------------------------------
void buildTopics() {
  snprintf(topicData,   sizeof(topicData),   "v1/%s/%s/%s/data",    BUILDING, FLOOR, ROOM_ID);
  snprintf(topicStatus, sizeof(topicStatus), "v1/%s/%s/%s/status",  BUILDING, FLOOR, ROOM_ID);
  snprintf(topicCmd,    sizeof(topicCmd),    "v1/%s/%s/%s/cmd",     BUILDING, FLOOR, ROOM_ID);
  snprintf(topicAck,    sizeof(topicAck),    "v1/%s/%s/%s/cmd/ack", BUILDING, FLOOR, ROOM_ID);
}

// ---------------------------------------------------------------------------
// Wi-Fi / 시각
// ---------------------------------------------------------------------------
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("WiFi 연결 중");
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("WiFi 연결됨. IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("WiFi 연결 실패 - 다음 루프에서 재시도");
  }
}

void syncTime() {
  // payload 의 timestamp 는 UNIX epoch(초, UTC)다. 오프셋 0으로 맞춰 UTC 를 그대로 쓴다.
  // NTP 동기화 전에 발행하면 1970년이 들어가 서버가 시각을 서버 기준으로 덮어쓴다.
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  Serial.print("시각 동기화 중");
  unsigned long start = millis();
  while (time(nullptr) < 1700000000 && millis() - start < 15000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  if (time(nullptr) >= 1700000000) {
    Serial.printf("시각 동기화 완료: %ld\n", (long)time(nullptr));
  } else {
    Serial.println("시각 동기화 실패 - 서버가 수신 시각으로 대체한다");
  }
}

// ---------------------------------------------------------------------------
// 센서 (HTTP 버전과 동일)
// ---------------------------------------------------------------------------
float readCurrentRMS() {
  long sumSq = 0;
  int midpoint = ADC_RESOLUTION / 2;  // 커플링 커패시터로 중간 바이어스된 경우 가정
  for (int i = 0; i < CT_SAMPLES; i++) {
    int raw = analogRead(PIN_CT);
    int centered = raw - midpoint;
    sumSq += (long)centered * (long)centered;
    delayMicroseconds(150);
  }
  float meanSq = (float)sumSq / CT_SAMPLES;
  float rmsRaw = sqrt(meanSq);
  float rmsVoltage = (rmsRaw / (float)ADC_RESOLUTION) * ADC_VREF;
  return rmsVoltage * CT_RATIO;
}

float readLux() {
  // LDR 전압 분배 회로 기준 — 실제 조도 환산은 소자 데이터시트로 보정할 것.
  int raw = analogRead(PIN_LDR);
  return ((float)raw / ADC_RESOLUTION) * 1000.0f;
}

bool readOccupancy() {
  // PIR: HIGH = 움직임 감지. 재실 유무만 사용 — 위치·영상 정보 없음 (계획서 4.4)
  return digitalRead(PIN_PIR) == HIGH;
}

void setRelay(bool on) {
  relayOn = on;
  // 릴레이 모듈이 active-LOW 인 경우가 많으니 배선 후 동작 방향을 확인해 반전 여부 조정할 것.
  digitalWrite(PIN_RELAY, on ? HIGH : LOW);
}

// ---------------------------------------------------------------------------
// MQTT
// ---------------------------------------------------------------------------
void onMessage(char* topic, byte* payload, unsigned int length) {
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, payload, length)) {
    Serial.println("[제어] 명령 파싱 실패");
    return;
  }

  const char* action = doc["action"] | "";
  const char* value  = doc["value"]  | "";
  const char* commandId = doc["command_id"] | "";

  // 지금 노드가 다루는 액추에이터는 조명(릴레이) 하나다.
  if (strcmp(action, "light") != 0) {
    Serial.printf("[제어] 다루지 않는 액추에이터: %s\n", action);
    return;
  }

  bool wantOn = (strcmp(value, "on") == 0);
  setRelay(wantOn);
  Serial.printf("[제어] 릴레이 -> %s (source=%s)\n",
                wantOn ? "ON" : "OFF", (const char*)(doc["source"] | "?"));

  // 실행 결과를 서버에 알린다. 이게 있어야 화면이 "응답 없음" 대신 완료를 표시한다.
  if (strlen(commandId) > 0) {
    StaticJsonDocument<128> ack;
    ack["command_id"] = commandId;
    ack["result"] = "COMPLETED";
    char body[128];
    size_t n = serializeJson(ack, body);
    mqtt.publish(topicAck, (const uint8_t*)body, n, false);
  }
}

bool connectMQTT() {
  if (WiFi.status() != WL_CONNECTED) return false;

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(512);

  Serial.printf("MQTT 연결 시도: %s:%d\n", MQTT_HOST, MQTT_PORT);

  // LWT: 노드가 정전·고장으로 죽으면 브로커가 대신 offline 을 발행해 준다.
  // retain=true 라 서버가 나중에 붙어도 마지막 상태를 알 수 있다.
  bool ok = mqtt.connect(DEVICE_ID, nullptr, nullptr,
                         topicStatus, 1, true, "{\"status\":\"offline\"}");
  if (!ok) {
    Serial.printf("MQTT 연결 실패 (rc=%d)\n", mqtt.state());
    return false;
  }

  // 연결 직후 online 을 알린다. LWT 와 짝을 이룬다.
  mqtt.publish(topicStatus, "{\"status\":\"online\"}", true);
  mqtt.subscribe(topicCmd, 1);

  Serial.println("MQTT 연결됨");
  Serial.printf("  발행: %s\n", topicData);
  Serial.printf("  구독: %s\n", topicCmd);
  return true;
}

void publishSensorData(bool occupancy, float lux, float currentAmp) {
  StaticJsonDocument<256> doc;
  doc["device_id"] = DEVICE_ID;
  doc["timestamp"] = (long)time(nullptr);

  JsonObject metrics = doc.createNestedObject("metrics");
  metrics["occupancy"] = occupancy;
  // 서버는 W 단위를 받는다. 전류 x 전압으로 환산해 보낸다.
  metrics["power"] = currentAmp * LINE_VOLTAGE;
  metrics["lux"] = lux;
  // 이 회로에는 온도 센서가 없다. 넣지 않으면 서버에서 null 로 남는다.

  char body[256];
  size_t n = serializeJson(doc, body);
  bool ok = mqtt.publish(topicData, (const uint8_t*)body, n, false);
  Serial.printf("[발행] %s occupancy=%d power=%.1fW lux=%.0f\n",
                ok ? "성공" : "실패", occupancy, currentAmp * LINE_VOLTAGE, lux);
}

// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_RELAY, OUTPUT);
  analogReadResolution(12);
  setRelay(true);

  buildTopics();
  connectWiFi();
  syncTime();
  connectMQTT();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
    delay(1000);
    return;
  }

  if (!mqtt.connected()) {
    // 지수 백오프까지는 필요 없다. 브로커가 잠깐 내려간 경우를 위한 단순 재시도.
    if (!connectMQTT()) {
      delay(3000);
      return;
    }
  }

  // 구독한 명령을 받으려면 계속 돌려줘야 한다.
  mqtt.loop();

  unsigned long now = millis();
  if (now - lastSampleAt >= SAMPLE_INTERVAL_MS || lastSampleAt == 0) {
    lastSampleAt = now;
    publishSensorData(readOccupancy(), readLux(), readCurrentRMS());
  }

  delay(50);
}
