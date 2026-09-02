--用户表
create table if not exists `oci_user`
(
    id                 varchar(64)                                     not null,
    username           varchar(64)                                     null,
    tenant_name        varchar(64)                                     null,
    tenant_create_time datetime                                        null,
    oci_tenant_id      varchar(64)                                     null,
    oci_user_id        varchar(64)                                     null,
    oci_fingerprint    varchar(64)                                     not null,
    oci_region         varchar(32)                                     not null,
    oci_key_path       varchar(256)                                    not null,
    create_time        datetime default (datetime('now', 'localtime')) not null,
    primary key ("id")
);
CREATE INDEX if not exists oci_user_create_time ON oci_user (create_time DESC);

--开机任务表
create table if not exists `oci_create_task`
(
    id               varchar(64)                                        not null,
    user_id          varchar(64)                                        null,
    oci_region       varchar(64)                                        null,
    instance_name    varchar(251)                                       null,
    ocpus            REAL        DEFAULT 1.0,
    memory           REAL        DEFAULT 6.0,
    disk             INTEGER     DEFAULT 50,
    boot_volume_vpus_per_gb INTEGER DEFAULT 20 not null,
    architecture     varchar(64) DEFAULT 'ARM',
    interval         INTEGER     DEFAULT 60,
    interval_max     INTEGER     DEFAULT 60 not null,
    availability_domain varchar(255),
    availability_domain_index INTEGER DEFAULT 0 not null,
    create_numbers   INTEGER     DEFAULT 1,
    initial_create_numbers INTEGER DEFAULT 1 not null,
    ssh_public_key   text        not null,
    operation_system varchar(128) DEFAULT 'Ubuntu',
    operation_system_version varchar(128),
    paused           INTEGER     DEFAULT 0,
    status           varchar(32) DEFAULT 'pending' not null,
    attempts         INTEGER     DEFAULT 0 not null,
    max_attempts     INTEGER     DEFAULT 1000 not null,
    last_error       text,
    next_run_at      datetime,
    updated_at       datetime    default (datetime('now', 'localtime')) not null,
    create_time      datetime    default (datetime('now', 'localtime')) not null,
    primary key ("id")
);
CREATE INDEX if not exists oci_create_task_create_time ON oci_create_task (create_time DESC);

--公网 IP 更换任务表
create table if not exists `oci_change_ip_task`
(
    id           varchar(64)  not null,
    user_id      varchar(64)  not null,
    instance_id  varchar(255) not null,
    vnic_id      varchar(255) not null,
    cidr_list    text,
    interval     INTEGER      DEFAULT 10 not null,
    status       varchar(32)  DEFAULT 'pending' not null,
    attempts     INTEGER      DEFAULT 0 not null,
    max_attempts INTEGER      DEFAULT 120 not null,
    last_error   text,
    create_time  datetime     default (datetime('now', 'localtime')) not null,
    updated_at   datetime     default (datetime('now', 'localtime')) not null,
    primary key ("id")
);
CREATE INDEX if not exists oci_change_ip_instance_status ON oci_change_ip_task (instance_id, status);
CREATE INDEX if not exists oci_change_ip_create_time ON oci_change_ip_task (create_time DESC);

--持久化通知发件箱
create table if not exists `notification_outbox`
(
    id              varchar(64)                                      not null,
    event_key       varchar(512)                                     not null,
    category        varchar(64)                                      not null,
    message         text                                             not null,
    status          varchar(32) DEFAULT 'pending'                    not null,
    attempts        INTEGER     DEFAULT 0                            not null,
    last_error      text,
    next_attempt_at datetime,
    created_at      datetime    default (datetime('now', 'localtime')) not null,
    updated_at      datetime    default (datetime('now', 'localtime')) not null,
    sent_at         datetime,
    primary key ("id")
);
CREATE UNIQUE INDEX if not exists notification_outbox_event_key
    ON notification_outbox (event_key);
CREATE INDEX if not exists notification_outbox_status_next_attempt
    ON notification_outbox (status, next_attempt_at);

--SSH 公钥表（私钥不持久化）
create table if not exists `ssh_public_key`
(
    id          varchar(64)                                      not null,
    name        varchar(128) COLLATE NOCASE                      not null,
    public_key  text                                             not null,
    fingerprint varchar(128)                                     not null,
    create_time datetime default (datetime('now', 'localtime'))  not null,
    update_time datetime default (datetime('now', 'localtime'))  not null,
    primary key ("id")
);
CREATE UNIQUE INDEX if not exists ssh_public_key_name ON ssh_public_key (name);
CREATE UNIQUE INDEX if not exists ssh_public_key_fingerprint
    ON ssh_public_key (fingerprint);
CREATE INDEX if not exists ssh_public_key_create_time
    ON ssh_public_key (create_time DESC);

create table if not exists `schema_migrations`
(
    version    INTEGER  not null,
    applied_at datetime default (datetime('now', 'localtime')) not null,
    primary key ("version")
);

--键值表
create table if not exists `oci_kv`
(
    id          varchar(64)                                     not null,
    code        varchar(64)                                     not null,
    value       text                                            null,
    type        varchar(64)                                     not null,
    create_time datetime default (datetime('now', 'localtime')) not null,
    primary key ("id")
);
CREATE INDEX if not exists oci_kv_code ON oci_kv (code DESC);
CREATE INDEX if not exists oci_kv_type ON oci_kv (type DESC);
CREATE INDEX if not exists oci_kv_create_time ON oci_kv (create_time DESC);
