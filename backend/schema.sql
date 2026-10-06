--
-- PostgreSQL database dump
--

\restrict GegmXdH4PoEpUJSUudgxetdsoZmEx9KkHS4icxcTCDnskDLn568wD3Nq5DNya2D

-- Dumped from database version 14.24 (Ubuntu 14.24-0ubuntu0.22.04.1)
-- Dumped by pg_dump version 14.24 (Ubuntu 14.24-0ubuntu0.22.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: prices; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.prices (
    area text NOT NULL,
    ts_dk timestamp with time zone NOT NULL,
    dkk_per_kwh numeric(8,4) NOT NULL,
    eur_per_kwh numeric(8,4),
    fetched_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.prices OWNER TO postgres;

--
-- Name: push_runs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.push_runs (
    run_id bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text NOT NULL,
    days_planned integer DEFAULT 0 NOT NULL,
    days_uploaded integer DEFAULT 0 NOT NULL,
    days_skipped integer DEFAULT 0 NOT NULL,
    bytes_uploaded bigint DEFAULT 0 NOT NULL,
    error text
);


ALTER TABLE public.push_runs OWNER TO postgres;

--
-- Name: push_runs_run_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.push_runs_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.push_runs_run_id_seq OWNER TO postgres;

--
-- Name: push_runs_run_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.push_runs_run_id_seq OWNED BY public.push_runs.run_id;


--
-- Name: pushed_state; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.pushed_state (
    day date NOT NULL,
    pushed_at timestamp with time zone DEFAULT now() NOT NULL,
    rows_pushed integer NOT NULL,
    content_hash text NOT NULL
);


ALTER TABLE public.pushed_state OWNER TO postgres;

--
-- Name: sync_runs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.sync_runs (
    run_id bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text NOT NULL,
    rows_fetched integer DEFAULT 0 NOT NULL,
    rows_written integer DEFAULT 0 NOT NULL,
    window_from timestamp with time zone,
    window_to timestamp with time zone,
    error text,
    job text DEFAULT 'dayahead_prices'::text NOT NULL,
    http_status integer,
    http_message text,
    error_url text,
    attempts integer,
    elapsed_ms integer,
    error_body jsonb
);


ALTER TABLE public.sync_runs OWNER TO postgres;

--
-- Name: sync_runs_run_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.sync_runs_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.sync_runs_run_id_seq OWNER TO postgres;

--
-- Name: sync_runs_run_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.sync_runs_run_id_seq OWNED BY public.sync_runs.run_id;


--
-- Name: sync_state; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.sync_state (
    job text NOT NULL,
    last_run_at timestamp with time zone NOT NULL,
    last_success_at timestamp with time zone,
    watermark_dk timestamp with time zone,
    rows_total bigint DEFAULT 0 NOT NULL
);


ALTER TABLE public.sync_state OWNER TO postgres;

--
-- Name: push_runs run_id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.push_runs ALTER COLUMN run_id SET DEFAULT nextval('public.push_runs_run_id_seq'::regclass);


--
-- Name: sync_runs run_id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sync_runs ALTER COLUMN run_id SET DEFAULT nextval('public.sync_runs_run_id_seq'::regclass);


--
-- Name: prices prices_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.prices
    ADD CONSTRAINT prices_pkey PRIMARY KEY (area, ts_dk);


--
-- Name: push_runs push_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.push_runs
    ADD CONSTRAINT push_runs_pkey PRIMARY KEY (run_id);


--
-- Name: pushed_state pushed_state_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.pushed_state
    ADD CONSTRAINT pushed_state_pkey PRIMARY KEY (day);


--
-- Name: sync_runs sync_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sync_runs
    ADD CONSTRAINT sync_runs_pkey PRIMARY KEY (run_id);


--
-- Name: sync_state sync_state_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.sync_state
    ADD CONSTRAINT sync_state_pkey PRIMARY KEY (job);


--
-- PostgreSQL database dump complete
--

\unrestrict GegmXdH4PoEpUJSUudgxetdsoZmEx9KkHS4icxcTCDnskDLn568wD3Nq5DNya2D

