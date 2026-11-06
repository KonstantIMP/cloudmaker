# Дополнительное задание

Здесь я сделал небольшой сервис на Python, который умеет создавать, запускать,
выключать и удалять ВМ через HTTP API. Сам сервис работает в докере, а виртуалки
запускаются на хосте через libvirt. Для этого в контейнер пробрасывается
`/run/libvirt`, через который он и общается с хостом.

Получается, что контейнер можно остановить или пересоздать, а ВМ продолжат
работать. При следующем запуске сервис снова увидит их через libvirt.
Рядом через compose поднимается prometheus, чтобы смотреть CPU, RAM и диск.

Реализованы дополнительные задания №1, 2, 5, 6, 7, 8, 11, 12.

## Как проверить (без подробностей)

Из корня репозитория перейдите в `extra` и запустите `prepare.sh` — он скачает
необходимый образ Debian 12:

```bash
cd extra
sudo ./prepare.sh
```

Поднимите приложение и prometheus:

```bash
sudo docker compose up -d --build --wait
```

Импортируйте скачанный образ в сервис:

```bash
curl -fsS localhost:8000/images \
  -H 'Content-Type: application/json' \
  -d '{"name":"debian-12","path":"debian-12-genericcloud-amd64-20261026-2643.qcow2","description":"Debian 12 genericcloud"}'
```

Скопируйте `id` из ответа и подставьте его вместо `REPLACE_WITH_IMAGE_ID`
в `examples/vm.json`. Создайте ВМ:

```bash
curl -fsS localhost:8000/vms \
  -H 'Content-Type: application/json' \
  -d @examples/vm.json
```

Выполните этот же запрос ещё раз — появится вторая ВМ. Посмотрите обе на хосте:

```bash
sudo virsh -c qemu:///system list --all
```

Возьмите `id` одной из ВМ из ответа API и подключитесь к её консоли:

```bash
sudo ./console.sh VM_ID
```

Дождитесь загрузки, логин и пароль — `cloudmaker:cloudmaker`. Внутри выполните:

```bash
cloud-init status --wait
hostname
cat ~/ready
```

Последняя команда должна вывести `ready`. Выход из консоли — `Ctrl+]`.

Откройте http://localhost:8000/metrics — там будут метрики ВМ. В prometheus
на http://localhost:9090 можно выполнить запрос `cloudmaker_vm_running`:
после сбора метрик появятся две ВМ со значением `1`.

![](./.img/Screenshot%202026-11-06%20at%2012.47.09.png)

Остановите и удалите контейнеры, затем проверьте, что обе ВМ всё ещё работают:

```bash
sudo docker compose down
sudo virsh -c qemu:///system list --all
```

Снова поднимите контейнеры и запросите список ВМ — сервис покажет те же две ВМ
с прежними `id`:

```bash
sudo docker compose up -d --wait
curl -fsS localhost:8000/vms
```

Когда закончите, удалите ВМ, скачанные образы, диски и контейнеры этой установки:

```bash
sudo ./clean.sh
sudo virsh -c qemu:///system list --all
sudo docker compose ps -a
```

Созданных ВМ и контейнеров приложения больше быть не должно.

## Что происходит при запуске

Из корня репозитория:

```bash
cd extra
sudo ./prepare.sh
sudo CLOUDMAKER_ACCEL=tcg docker compose up -d --build
```

Задача `prepare.sh` - скачать и импортировать в сервис базовой образ.
Тут уже обычный debian 12, но нужен был cloud init и я решил больше жизнь не усложнять.

По умолчанию ВМ запускаются с KVM. Если он недоступе (я работаю на маке,
поэтому реализована вот такая штука), можно использовать TCG:

```bash
sudo CLOUDMAKER_ACCEL=tcg docker compose up -d --build
```

## Образы ВМ

Для запуска ВМ ей необходим образ. 

Можно добавлять образы, но у этого процесса есть ряд ограничений.
Скачать образ нужно самостоятельно и положить его обязательно в
`/var/lib/libvirt/images/cloudmaker/images` на хосте. Пути на хосте и внутри
контейнера должны совпадать, чтобы сервис и libvirt видели один и тот же файл.

Образ должен поддерживать **cloud-init с источником NoCloud** — через него
сервис настраивает ВМ при первом запуске.

Например, скачать Debian 12 руками можно так (это же делает `prepare.sh`):

```bash
sudo mkdir -p /var/lib/libvirt/images/cloudmaker/images
sudo curl -fL \
  https://cloud.debian.org/images/cloud/bookworm/20261026-2643/debian-12-genericcloud-amd64-20261026-2643.qcow2 \
  -o /var/lib/libvirt/images/cloudmaker/images/debian-12-genericcloud-amd64-20261026-2643.qcow2
sudo chmod 644 /var/lib/libvirt/images/cloudmaker/images/debian-12-genericcloud-amd64-20261026-2643.qcow2
```

После скачивания импортируем образ:

```bash
curl -s localhost:8000/images \
  -H 'Content-Type: application/json' \
  -d '{"name":"debian-12","path":"debian-12-genericcloud-amd64-20261026-2643.qcow2","description":"Debian 12 genericcloud"}'
```

В `path` указывается имя файла внутри каталога `images`. В ответе будет `id`
образа, он понадобится для создания ВМ. Другие образы добавляются так же,
нужно только поменять имя и путь к файлу.

Исходный образ нельзя удалять или заменять, пока от него есть ВМ:
их диски хранят только изменения и ссылаются на него.

## Как создать ВМ

Нужно выполнить запрос к api, формат можно подсмотреть в [examples/vm.json](./examples/vm.json). Потом:

```bash
curl -s localhost:8000/vms \
  -H 'Content-Type: application/json' \
  -d @examples/vm.json
curl -s localhost:8000/vms
```

По умолчанию у ВМ будет 1 CPU, 512 МиБ RAM и диск на 8 ГиБ. Можно задать
`vcpus`, `memory_mb` и `disk_gb` в запросе; диск должен быть не меньше исходного
образа.

Для настройки гостя сервис собирает отдельный ISO с cloud-init конфигом и
подключает его к ВМ. В `user_data` можно передать свой cloud-config, а в
`ssh_keys` — список публичных SSH-ключей для пользователя `cloudmaker`.

В примере я ещё задал пароль, чтобы можно было зайти через serial-консоль:

```bash
sudo ./console.sh VM_ID
```

Здесь `VM_ID` — это `id` из ответа на создание ВМ. Логин и пароль в примере —
`cloudmaker:cloudmaker`. Без настроек из примера вход по паролю заблокирован.
После входа можно проверить, что cloud-init всё сделал:

```bash
hostname
cloud-init status --wait
cat ~/ready
```

Выйти из консоли — `Ctrl+]`, сама ВМ при этом продолжит работать.
Сеть у гостя — user networking QEMU, проброса портов наружу пока нет.

Остальные действия через API:

| Запрос | Что делает |
|---|---|
| `GET /images` | Список зарегистрированных образов |
| `GET /vms` | Список ВМ |
| `GET /vms/{id}` | Состояние одной ВМ |
| `POST /vms/{id}/stop` | Выключить ВМ; если за 20 секунд не выключилась, остановить принудительно |
| `POST /vms/{id}/start` | Запустить ВМ с прежним диском |
| `DELETE /vms/{id}` | Удалить ВМ вместе с её диском |

На хосте их также видно через `sudo virsh -c qemu:///system list --all`.

## Мониторинг и проверка

Prometheus доступен на http://localhost:9090 и поднимается docker compose-ом,
метрики сервиса — на http://localhost:8000/metrics. Например, можно посмотреть:

- `rate(cloudmaker_vm_cpu_seconds_total[1m])` — использование CPU;
- `cloudmaker_vm_memory_rss_bytes` — RAM процесса QEMU;
- `cloudmaker_vm_disk_allocated_bytes` — занятое место в диске с изменениями.

То есть RAM и диск здесь измеряются со стороны хоста, свободную память и
место внутри гостевой ОС эти метрики не показывают.

Для полной проверки есть `sudo ./check.sh`. Он поднимает отдельную установку
на портах 18000/19090, создаёт ВМ, проверяет cloud-init, сохранение данных,
пересоздание контейнера и метрики, а потом всё за собой удаляет.

Обычный `docker compose down` оставляет ВМ и данные. После перезагрузки хоста
ВМ нужно снова запустить через API. Если нужно удалить всю установку вместе
с её ВМ, образами, дисками и томами Docker:

```bash
sudo CLOUDMAKER_ACCEL=tcg ./clean.sh
```
